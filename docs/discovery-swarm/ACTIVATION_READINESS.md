# Activation readiness + operator runbooks

2026-07-11 · Integrated main commit `b25166a` (== origin/main, deployed dark). **No Ross-gated production change has been performed.** All activations below are operator steps for Ross to perform/approve in Render; each is separately observable.

## Configuration verification (main HEAD `b25166a`)

| # | Configuration | Result |
|---|---|---|
| 1 | `GUILD_STORE=json`, hashing OFF (**current production**) | **407 passed, 1 skipped** |
| 2 | `GUILD_STORE=sqlite`, hashing OFF | **407 passed, 1 skipped** |
| 3 | `GUILD_STORE=sqlite`, `GUILD_HASH_KEYS=1`, `GUILD_ALLOW_WEAK_KDF=1`, `GUILD_KDF_ITERS=1000` | **407 passed, 1 skipped** |

The 1 skip is `test_claimcheck.py` (the fact-check supplier, gated behind `GUILD_ENABLE_CLAIMCHECK` — intentionally dark, Step 6 caveat). Concurrency suite 18/18, retry-idempotency 8/8, credential leak scan clean, reachability verifier 30/30 — all inside the above runs.

Live production snapshot for the runbooks: **53 agents**, **52 legacy-scope credentials** (all current custodial keys — none carry an explicit `scopes` field until migrated/rotated). Persistence today: JSON at `/data/guild.json` (+ `.events.jsonl`) on the Render disk.

Ordering rule: **each step is deployed and observed separately.** Do not combine SQLite cutover (Step 3) with credential activation (Step 4).

---

## Step 1 — First-party authentication (`GUILD_FIRST_PARTY_TOKEN`)

Full detail: `FIRST_PARTY_TOKEN.md`. Code already enforces exact-match when the var is set (both `app/main.py` and `app/swarm/router.py`).

**Generate (locally, do not paste into logs/docs/chat):**
```
openssl rand -hex 24
```
Store it in a password manager. Do **not** reuse `GUILD_ADMIN_TOKEN`.

**Update every AG-owned caller BEFORE setting it in Render** (else our own jobs degrade to external-unknown until updated):
- ops-watch scheduled task (07:06) — send header `X-Guild-Source: <token>`.
- growth-sprint scheduled task (10:05) — same.
- MCP canary (`live/scripts/mcp_canary.py`) — reads `GUILD_FIRST_PARTY_TOKEN` from env or `live/secrets/first_party_token`; give it the token.
- seed/verification tooling and any manual curl recipes.

**Set:** Render → service env → `GUILD_FIRST_PARTY_TOKEN` = value → deploy (single instance stop-then-start).

**Verify (Ross or approved):**
1. A request with the OLD arbitrary header (`X-Guild-Source: test`) now lands `first_party: false` in `/instrumentation/recent`.
2. A request with the token lands `first_party: true`.
3. AG jobs classify `AG_INTERNAL`/`AG_TEST` (check `/instrumentation.caller_classes`).
4. External-growth metrics (`genuine_external_*`) unchanged vs the pre-set snapshot — the analytics invariant already excludes AG_*, so this should be a no-op for external counts; confirm it is.

**Rollback:** unset `GUILD_FIRST_PARTY_TOKEN` and redeploy → reverts to the (honor-based) any-non-empty-header behavior. Non-destructive.

---

## Step 2 — MCP canary (authenticated, `AG_TEST`)

Script exists and is green in production: `live/scripts/mcp_canary.py` (UA `mcp:guild-canary/1` → AG_TEST by `attribution.AG_TEST_UA_RE`; sends the first-party token). It checks: initialize, tools/list, harmless invocation (`ag_json_canonicalize`), malformed invocation (machine-readable error), host protection (spoofed Host → 421/403/404), origin protection (foreign Origin → 403), and per-check latency. Exit 0 healthy / 1 failed.

**Canary traffic never contributes to genuine-external metrics** — the AG_TEST UA is excluded by `is_genuine_external`/`may_count_as_external_growth` (the central analytics invariant, tested).

**Schedule** (after Step 1, so it authenticates with the real token): create a scheduled task that runs `python live/scripts/mcp_canary.py` every ~15 min and alerts on exit 1. This closes the gap that let the 421 regression persist for days. (I can create this scheduled task on request — it only runs a read-only health probe against production and does not need a Ross Render action.)

---

## Step 3 — SQLite cutover runbook

Detail + topology + the three-layer guard: `SQLITE_CUTOVER.md`. Schema map: `SQLITE_SCHEMA.md`. **Do not activate until the backup + dry-run are reviewed.**

Exact operator sequence (paths are the Render disk mount):
```
# 0. Confirm topology unchanged: single instance (disk attached), CMD has no --workers.

# 1. BACKUP (production, on the /data disk)
cp /data/guild.json                /data/guild.json.bak.$(date +%Y%m%d)
cp /data/guild.json.events.jsonl   /data/guild.json.events.jsonl.bak.$(date +%Y%m%d)   # if present

# 2. MIGRATE to a NEW file (source is never modified)
python live/scripts/migrate_json_to_sqlite.py --data /data/guild.json --out /data/guild.db

#    Expected report (RESULT: verified OK) covers, per the amendments:
#      - entity counts (all 16 collections) sqlite == source
#      - canonical hashes match (agents, ledger)
#      - ledger chain intact + heads match (sqlite and source)
#      - account->agent / escrow->agent orphans: 0
#      - duplicate ledger.seq / attestation.id: 0
#      - credential-field preservation (verifier-format + key_id); raw-key
#        end-to-end auth only where a raw key exists (see note below)
#      - reachability records preserved
#      - integrity_check: ok

# 3. VERIFY-ONLY re-run (idempotent, re-checks an existing DB)
python live/scripts/migrate_json_to_sqlite.py --data /data/guild.json --out /data/guild.db --verify-only

# 4. CUTOVER (quiet window; deploy stop-then-starts the single instance)
#    Render env: GUILD_STORE=sqlite  and  GUILD_DATA=/data/guild.db   -> deploy

# 5. PRODUCTION PROBES after restart
curl -s $BASE/health
curl -s "$BASE/check?capability=fact-check" | jq .reachability.status   # supply_has_no_declared_endpoint
# a register->rotate->revoke round-trip on a throwaway agent; confirm auth works
```

Credential note for Step 3 migration: production keys are currently **plaintext-at-rest** (hashing is OFF), so the migration CAN end-to-end authenticate them (raw key present). Once hashing is on (Step 4), migration can only assert verifier-format + key_id preservation unless a `--test-key` is supplied — the report says so honestly.

**Rollback trigger:** any failed probe, integrity error, or auth failure post-cutover.
**Rollback command:** set `GUILD_STORE=json` (and `GUILD_DATA=/data/guild.json`) → redeploy. **Caveat:** writes that landed in SQLite after cutover are not in the JSON file; keep the window short/low-traffic, or re-migrate forward rather than back. No reverse-migration script exists (flagged).

**After activation, observe the service before any credential migration (Step 4).**

---

## Step 4 — Credential-hashing activation runbook (SEPARATE from Step 3)

Detail + rollout lifecycle: `CREDENTIALS_DESIGN.md` §15. Dry-run figures: `PILOT_A_COMPLIANCE.md` / earlier report (20/20 hashed, 0 raw keys on disk/journal, ~29 ms/auth at 100k iters). **One-way migration — never combine with the SQLite cutover.**

```
# 1. FRESH BACKUP (whichever store is live at this point)
cp /data/guild.json /data/guild.json.bak.creds.$(date +%Y%m%d)     # json mode
# or  cp /data/guild.db /data/guild.db.bak.creds.$(date +%Y%m%d)   # sqlite mode

# 2. LEGACY CREDENTIAL COUNT (baseline)
curl -s -H "X-Guild-Source: <token>" $BASE/instrumentation | jq .legacy_scope_credentials.count   # 52 today

# 3. MIGRATION DRY-RUN (local, against a COPY of the backup)
#    load the copy with GUILD_HASH_KEYS=1 -> _migrate_plaintext_keys runs; confirm
#    0 plaintext remain, all keys authenticate, no raw sk_ on disk/journal.

# 4. ENABLE (quiet window)
#    Render env: GUILD_HASH_KEYS=1   -> deploy. Migration runs on load.

# 5. AUTHENTICATION PROBES
#    a throwaway agent's key still authenticates; a bogus key 401s;
#    a bare key_id is never accepted.

# 6. LEAK SCAN against the live state file (no raw sk_ in state/journal/events).

# 7. LATENCY: confirm auth ~30ms at GUILD_KDF_ITERS default 100000 (bounded, tunable).
```

Production KDF must use the real floor (100k) — do **not** set `GUILD_ALLOW_WEAK_KDF` in production (that gate exists only for tests).

**Rollback:** hashing is one-way. Restore the Step-4 backup and unset `GUILD_HASH_KEYS`. Keys issued after enabling are not in the backup → enable in a quiet window.

---

## Step 5 — Registry refresh

Status + current records: `REGISTRY_VERIFICATION.md`. Staged corrected A2A card: `harness/results/a2aregistry_entry_corrected.json` (19 skills, protocol 0.3.0).

| Registry | Current published metadata | Points at current prod endpoint? | Action |
|---|---|---|---|
| MCP official registry | `io.github.AgentTanuki/agent-guild` v1.0.0 + v1.1.0 (latest) | YES — `…/mcp` (handshake verified after the 421 fix) | Optionally deprecate v1.0.0. Description current. |
| a2aregistry.org | name "Agent Guild", `…/a2a`, wellKnownURI `…/agent-card.json`; health WORKING | Endpoint YES; **stored skills stale (2 vs live 19)** | **Submit the corrected card** (PR to their git-as-database repo). Outbound public content → Ross performs/approves. |
| Smithery | `agent-tanuki/agent-guild`; **stored tools stale (5 vs live 30)** | Gateway proxied (401 without account key) | **Re-scan/re-publish** via Smithery account. Ross performs. |
| Glama | slug `agent-guild` | YES (search-findable) | None. |

**Discoverability rule:** a submission does not count as discoverable until the registry has **independently indexed** it and it is **searchable**, pointing at the **current** production endpoint + schema. Verify each after submission before counting it.

---

## Step 6 — Cold-discovery gate (run after Step 5)

Harness: `harness/cold_discovery.py` (contains no provider hostname; clean client state; budget + time limit). After the listing refresh, run an independent **registry-led** attempt: no preloaded AG URL, no AG-specific prompt, a real task, full evidence capture (`harness/results/`). Target: at least one registry-led path reaches AG's refreshed card and completes an invocation — the currently-missing win (protocol-led already passes; registry-led missed only because of the stale listing text).

### Pilot B → GO criteria (all required)
1. Service stable on the selected persistence backend (json now, or sqlite post-cutover, observed).
2. First-party traffic classification reliable (Step 1 verified).
3. MCP canary active (Step 2 scheduled).
4. Credentials safely activated (Step 4) **or** an explicit decision to delay them, recorded.
5. One registry-led cold-discovery path succeeds end to end (Step 6).

### Fact-check supplier — hold
Do **not** publish `evidence.claim_check` (set `GUILD_ENABLE_CLAIMCHECK=1`) until the external-actor natural experiment (`evidence/external-actor-4580505b.md`) has reached its **pre-recorded observation point** — hypothesis: the reachability fix stops the polling; falsifier: polling continues. The actor has not returned since 08:36 UTC 2026-07-10; keep observing passively, do not contact it, and do not introduce supply that would contaminate the experiment.
