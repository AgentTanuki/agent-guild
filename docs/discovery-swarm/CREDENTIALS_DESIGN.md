# Credential Hardening Design — hashed-at-rest api keys, key_ids, scopes

Status: implemented on branch `credential-hardening`, **NOT deployed**.
Date: 2026-07-10 (Pilot A security audit follow-up).
Code: `app/credentials.py` (all primitives), `app/store.py` (storage,
migration, resolution), `app/main.py` / `app/swarm/gateway.py` /
`app/mcp_server.py` (enforcement points).
Tests: `tests/test_credential_hardening.py` (both modes, migration, scopes,
rotation, revocation, no-secret-in-journal).

## 1. Problem (audited 2026-07-10)

Agent api keys (`sk_*`) were stored **plaintext** in three places: the agent
record (`store.py register_agent`), as billing-account dict keys, and verbatim
as actor keys in the events journal. Auth was raw string equality
(`main._require_key`, `gateway.derive_actor`, `mcp guild_attest` linear scan).
One leaked data file or journal line = every custodial credential compromised.
No scopes, no expiry.

## 2. Feature flag: `GUILD_HASH_KEYS`

* **unset / not `"1"` (default): behavior is byte-for-byte today's.** No
  hashing, no migration, plaintext storage, raw-key account/event keys. The
  branch is safe to deploy dormant.
* **`"1"`: hashed-at-rest.** Everything below activates. The flag is read at
  call time (`credentials.hashing_enabled()`), never cached at import.

## 3. Hashed-at-rest keys

* On issue (register / rotate) the record stores only
  `api_key_hash = sha256(raw)` and drops `api_key` to `None`. sha256 is
  adequate for 192-bit random tokens (`sk_` + 24 random bytes hex); there is
  nothing to dictionary-attack, so no bcrypt/argon2 cost is warranted, and a
  fast hash keeps per-request verification O(1)-cheap.
* **The raw key is shown exactly once**: `register_agent` returns a one-time
  copy carrying `api_key`; the stored record never holds it. Rotation returns
  the new raw key once, same rule.
* **Verification is constant-time** (`hmac.compare_digest` over the hash hex;
  legacy plaintext records compare_digest raw) — `credentials.verify_agent_key`
  is the single verifier used by `_require_key`, `derive_actor`,
  `_prove_auth`, attestation issue, `/collaborations`, `/demand/watch`,
  `_is_self_read` and `store.agent_for_presented_key`.

## 4. Public `key_id`

`key_id = sha256(raw)[:12]` — stable, non-reversible, safe to publish.
It replaces the raw key as:

* the billing-account dict key (account marked `"hashed": true`),
* the actor key on every instrumentation event (`record_event` sanitizes ANY
  `sk_`-prefixed key — even unknown/mistyped probes are logged pseudonymously),
* the swarm member actor key and escrow `requester_key`.

**A bare key_id is never a credential.** Account resolution
(`store._account_key`) only maps a *raw* `sk_` secret onto a hashed account
(via its hash); presenting the public key_id fails auth, billing, escrow and
member-tier checks (tested).

## 5. Scopes

Closed vocabulary: `read, invoke, attest, escrow, admin`
(`credentials.SCOPES`). `scopes` list on the agent record; **absent field or
fresh issue = ALL scopes**, so every existing record and integration keeps
working unchanged. Enforcement (`_require_scope` / `credentials.has_scope`,
active in BOTH modes since the default is permissive):

| Surface | Scope |
|---|---|
| member-tier `POST /invoke/{cap}` (REST, MCP ag_* tools, A2A `invoke:` — all via `gateway.derive_actor`) | `invoke` |
| `POST /attestations` (custodial path) + MCP `guild_attest` | `attest` |
| `POST /escrow`, `/release`, `/refund`, `/dispute` (via `_require_account`) | `escrow` |
| `POST /agents/{id}/key/rotate` and `/key/revoke` (self-serve; admin token bypasses) | `admin` |

Denial is a machine-readable 403:
`{"error":"missing_scope","required_scope":"attest","have_scopes":[...],"agent_id":"..."}`.
A valid key lacking `invoke` is denied explicitly at the gateway, never
silently downgraded to guest. Guests (no key) are unaffected.

## 6. Optional expiry

`api_key_expires_at` (ISO-8601) on the record; `verify_agent_key` fails past
it. Set via `store.rotate_api_key(agent_id, expires_in_days=...)`; rotation
without the parameter clears expiry. Default: none (keys live until
rotated/revoked — today's behavior).

## 7. Audit events (key_id only, never the secret)

* `api_key_issued` (hashed mode, at register): key_id + credential_class.
* `api_key_rotated`: new actor key, `key_id`, `old_key_id`.
* `api_key_revoked`: `key_id` retained on the record as public history.
* `api_keys_migrated`: count, emitted once by the migration.

## 8. First-party vs external credential marking

`credential_class: "first_party" | "external"` stamped on the record at issue
and at migration (derived from the existing `first_party` flag), so credential
inventory can be audited separately from traffic attribution.

## 9. Migration of existing plaintext keys

`Store._migrate_plaintext_keys()`, runs **only** under `GUILD_HASH_KEYS=1`, on
first load (`_load`), in place and idempotently:

1. each plaintext `api_key` → `api_key_hash` + `key_id`, scopes/class stamped;
2. billing account re-keyed raw → key_id (balances, recs, ownership intact);
3. historical actor keys rewritten raw → key_id in `events` (key + actor
   fields), `billing_log`, `escrows.requester_key`, swarm referral tokens;
4. one `_save()` — which also compacts the events journal — so **no raw key
   remains on disk** afterwards (tested).

Agents keep authenticating with the key they already hold (it hashes to the
stored digest); attribution history stays continuous under the key_id.
Rollback before deploy commit: turning the flag off stops verification-mode
changes but does NOT un-hash records (raw keys are unrecoverable by design);
rollback therefore means restoring the pre-migration data file backup.

## 10. What stays decryptable, and why

**Custodial ed25519 private keys only** (`private_key` on custodial records).
The Guild signs attestations/receipts *on behalf of* custodial agents
(`add_custodial_attestation`, passports, ledger records) — signing requires
the actual key material, so it cannot be one-way hashed. It is a deliberate,
documented custody feature (the record already says "secret; custodial
only") and is excluded from this hardening. Mitigations stay: never serialized
into profiles/passports/ledger entries, never logged, and self-sovereign
registration (bring your own public key) avoids custody entirely.
api keys, by contrast, are pure bearer verifiers — nothing legitimate ever
needs to read them back, so they are hashed.

## 11. No-secret-in-logs guarantee

* `record_event` is the single event chokepoint and sanitizes every
  `sk_`-prefixed actor key to its key_id when the flag is on — including keys
  that match no agent (probes, typos), so a fat-fingered secret can't leak.
* Rotation/revocation/issue audit events carry key_ids only.
* The ledger dual-write already excluded `private_key`/`api_key` (public
  fields only); unchanged.
* Tested: `json.dumps(store.events)` contains no raw key after register,
  metered reads, attestation attempts, rotation; disk files (store + journal)
  contain no raw key after migration.

## 12. Compatibility notes

* Default-off flag: OFF suite is the pre-change suite (284 tests green).
* ON changes only *internal* representations; the wire API is identical
  (register/rotate still return `api_key` once).
* Two existing tests peeked at internal storage (account keyed by raw key,
  gateway actor == raw key) and were made mode-aware.
* Behavioral fix rolled in: key rotation now migrates the payer's OPEN
  (funded) escrows to the new credential, so post-rotation release/refund
  requires the NEW key. Previously the retired key string still matched the
  escrow record (a replay hole) while the new key did not.

## 13. Verifier construction (refinement, 2026-07-10)

The first cut of this branch stored a **raw, unsalted SHA-256** of the key.
That is replaced by a salted PBKDF2-HMAC-SHA256 verifier, self-describing so
the cost can be raised later without a migration:

```
pbkdf2_sha256$<iterations>$<salt_b64>$<dk_b64>
```

* Per-key random 16-byte salt (defeats precomputation and cross-key
  correlation). Default 100 000 iterations (`GUILD_KDF_ITERS`, tunable).
* Rationale for PBKDF2 over argon2/bcrypt: api keys are 192-bit random
  (`sk_` + 24 random bytes), so brute force is already infeasible and a heavy
  memory-hard KDF only adds auth latency; PBKDF2 gives defense-in-depth at a
  bounded cost (a single verify measured < 100 ms at 100k iters — tested).
* `verify_key_hash` is constant-time (`hmac.compare_digest`) and also accepts a
  legacy bare-sha256 digest, so a record hashed by the first cut of this branch
  still authenticates during a rolling upgrade and re-hashes to PBKDF2 on its
  next rotation.
* **key_id is a separate, deterministic identifier** — `sha256(key)[:12]`,
  used ONLY to look up the record in O(1). It is not a secret verifier (48
  bits, non-reversible to the 192-bit key), safe to log and to use as an
  account/event key, and is never accepted as a credential.

## 14. Scopes — least privilege (refinement)

* Vocabulary: `read, invoke, attest, escrow, admin`. **Unknown scopes fail
  closed** (`has_scope` returns False for anything outside the vocabulary,
  even for a legacy all-scopes record).
* **Newly issued credentials default to `DEFAULT_ISSUE_SCOPES = read, invoke,
  attest, escrow`** — every action a normal member legitimately performs, but
  NEVER `admin`. `admin` is reserved for the operator path (admin token) and is
  never granted to a self-registered key.
* A pre-scoping record (no `scopes` field) is treated as fully privileged until
  its next rotation re-issues it with least-privilege scopes — this is the only
  place "all scopes" is a default, and it exists purely for backward
  compatibility.
* **Self-service rotation/revocation does NOT require `admin`.** Authenticating
  with the agent's own current key proves ownership; requiring `admin` there
  (as the first cut did) would make autonomous credential lifecycle impossible.
  The admin TOKEN remains the recovery path after a revoke.
* Route-level scope requirements are explicit and testable: member-tier
  `/invoke` → `invoke`; `/attestations` + MCP `guild_attest` → `attest`;
  `/escrow*` → `escrow`.

## 15. Rollout lifecycle (staged, toward hashed as the end-state)

Intended production end-state: hashed credentials, plaintext storage removed,
flag deleted. Sequence:

1. **Ship dark (now).** `GUILD_HASH_KEYS` unset in production → zero behavior
   change; the code path is exercised only by the ON-mode test suite.
2. **Backup.** Snapshot `guild.json` (+ `.events.jsonl`) — migration is one-way.
3. **Enable.** Set `GUILD_HASH_KEYS=1`. On the next load, `_migrate_plaintext_keys`
   rewrites every plaintext key in place: record → hash + key_id, account and
   historical event/billing/escrow keys → key_id, one `_save` compacts the
   journal so no raw key remains on disk. Idempotent (a re-load re-migrates
   nothing). Emits an `api_keys_migrated` audit event.
4. **Mixed-record recognition.** A record is hashed iff it has `api_key_hash`
   (and `api_key is None`); plaintext iff it has a non-null `api_key`.
   `verify_agent_key` handles both, so during the load window there is never a
   half-state visible to callers. New issues are always hashed under the flag.
5. **Rollback.** Because hashing is one-way, rollback = restore the pre-migration
   backup (step 2) and unset the flag. Any keys issued AFTER enabling would not
   be in the backup, so roll back promptly or re-issue those few. Documented as
   the reason to enable during a quiet window.
6. **Remove plaintext.** Once `/instrumentation` (or a one-off audit query)
   shows zero records with a non-null `api_key`, the plaintext read paths in
   `verify_agent_key`/`actor_key_for_agent` become dead and can be deleted.
7. **Delete the flag.** After plaintext removal has been live and stable, make
   hashing unconditional (drop the `hashing_enabled()` branches), delete
   `GUILD_HASH_KEYS`, and drop the legacy bare-sha256 acceptance in
   `verify_key_hash`. The `key_id`/scopes/audit machinery stays.

## 16. Credential-leak regression scan

`tests/test_credential_leak_scan.py` asserts, under the flag: no raw `sk_`
secret in JSON state, the journal, event records, the billing log, application
logs (`caplog`), or exception strings, after driving register / metered read /
attestation / escrow-failure / rotation / revocation; plus a static repo scan
for stray raw-key literals outside tests; plus the KDF-latency bound. When the
SQLite backend lands, the same scan is extended to the SQLite state file.
