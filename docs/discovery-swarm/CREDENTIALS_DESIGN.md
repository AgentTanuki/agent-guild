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
