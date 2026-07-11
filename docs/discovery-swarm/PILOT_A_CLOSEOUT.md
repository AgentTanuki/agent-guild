# Pilot A closeout

**PILOT A: COMPLETE. Technical foundation validated.**
**PILOT B: CONDITIONAL GO — pending operator activation and one successful registry-led cold-discovery path.**

Date: 2026-07-11 · Final integrated commit: `a9d0380` (pushed, deployed) · Baseline: `PILOT_A_METRICS_BASELINE.md`.

## A. Objective

Pilot A tested whether Agent Guild can operate as credible **machine-native infrastructure** across: agent identity, machine discovery, invocation, attribution, reachability, trust, credentials, persistence, ledger integrity, and readiness for future controlled swarm activity — validated by measured external-machine behaviour, not architectural narrative.

## B. Achievements

- **Production MCP** — standards-compliant handshake, tool discovery, and invocation (after root-causing and fixing a `421` that had silently killed the endpoint); verified by an external clean-context client and a scheduled canary.
- **A2A + machine-readable discovery** — Agent Card, `/a2a` JSON-RPC, `/.well-known/*`, `llms.txt`, capability index; discoverable via the MCP registry, a2aregistry, Glama, Smithery.
- **Recurring external-machine engagement** — actor `a2a:net:4580505b` returned across days with deciding requests (engagement + recurrence supported; §D for what it does *not* prove).
- **Reachability-honest `/check`** — no more un-actionable "hire X" recommendations; honest `supply_has_no_declared_endpoint` + an actionable `/demand/watch` path.
- **Machine menu continuation** — bare option replies now return a structured clarification instead of a dead-end `probe_ack`.
- **Clean internal/external attribution** — closed 7-class caller taxonomy; a central invariant makes AG_INTERNAL/AG_TEST/OPERATOR/REGISTRY_CRAWLER structurally unable to touch external-growth metrics; canary/probe UAs excluded (verified in production).
- **Credential hardening** — salted PBKDF2 at rest, 128-bit key_id, least-privilege scopes, self-service rotate/revoke, credential-leak scan (flag-gated, dark).
- **Evidence-based endpoint verification** — SSRF-safe declaration-time reachability verifier with a real evidence ladder; `invocation_verified` only from a trusted AG-originated bound invocation, never a receipt.
- **Transactional SQLite backend** — database-authoritative writes (`BEGIN IMMEDIATE` authoritative reads + version/CAS), derived-and-reconciled revenue, per-thread connections, WAL/FK/busy_timeout (flag-gated, dark).
- **Multi-process concurrency guarantees** — concurrent registrations lose 0 (vs JSON 74%); exactly-one-credential rotation; contiguous ledger seq; exact financial accounting — proven with real OS processes.
- **Retry idempotency** — fault-injected proof that a retried transaction commits exactly once or fails explicitly (no duplicated events/ledger/billing/receipts/escrow/revenue/rotation).
- **Operator runbooks** — `ACTIVATION_READINESS.md` + step docs for every gated activation.
- **Natural experiment preserved** — the external actor was never contacted, stimulated, or impersonated throughout.

## C. Final evidence

- **Final main commit:** `a9d0380` — pushed to origin, deployed to Render (health ok; 16 identities = claimcheck OFF; caller_classes live).
- **Test results on `a9d0380`:**
  1. `GUILD_STORE=json`, hashing OFF (production config): **407 passed, 1 skipped**
  2. `GUILD_STORE=sqlite`, hashing OFF: **407 passed, 1 skipped**
  3. `GUILD_STORE=sqlite`, `GUILD_HASH_KEYS=1` (permitted test KDF): **407 passed, 1 skipped**
- **Final post-fix full-suite count (json, incl. the attribution regression test):** **408 passed, 1 skipped** (the skip is the dark fact-check supplier).
- **Concurrency suite:** 18/18. **Retry-idempotency suite:** 8/8. **Credential leak scan:** clean (state/journal/events/billing/logs/exceptions + static repo scan). **Reachability verifier:** 30/30 (incl. real TLS-pinning integration).
- **Production MCP result:** canary healthy — initialize 200, tools/list (30 tools), harmless invoke ok, malformed → machine-readable error, host guard 403, origin guard 403, latency captured.
- **Production canary-attribution result:** the canary's live events classify `ag_test` / `genuine_external:false`; running it moved **no** genuine-external or acquisition metric (events 137→137, unique 3→3, registrations 14→14; AG_TEST 65→67).
- **Historical metric correction result:** genuine-external set audited directly in production — 129/500 recent events are genuine, resolving to exactly two external `python-httpx` A2A callers (79 anonymous poller + 50 `a2a:net:4580505b`); **zero** AG-owned probe UAs in the genuine set. Correction is read-time (`PILOT_A_METRICS_BASELINE.md`).

## D. What Pilot A did NOT prove

- Actor `4580505b`'s **original discovery route remains unknown**.
- **Registry-led cold discovery has not yet succeeded end to end** (protocol-led did; registry-led missed only because the a2aregistry/Smithery listing text is stale).
- **No completed external demand→supplier transaction** has occurred.
- The **fact-check supplier remains unpublished** (dark).
- The **controlled swarm has not launched**.
- **Production still uses JSON** unless the operator performs the SQLite cutover.
- **Credential hashing remains dark** unless activated.

## E. Remaining operator-gated actions (Pilot B *entry* actions, not unfinished Pilot A architecture)

1. Set `GUILD_FIRST_PARTY_TOKEN`.
2. Schedule the MCP canary.
3. Cut over to SQLite (`GUILD_STORE=sqlite`).
4. Activate credential hashing (`GUILD_HASH_KEYS=1`) — separately from (3).
5. Refresh the a2aregistry and Smithery listings.
6. Run a clean registry-led cold-discovery test.

Runbooks: `ACTIVATION_READINESS.md`, `FIRST_PARTY_TOKEN.md`, `OPS_HARDENING.md`, `SQLITE_CUTOVER.md`, `SQLITE_SCHEMA.md`, `CREDENTIALS_DESIGN.md`, `REGISTRY_VERIFICATION.md`.

## F. Pilot B gate

Pilot B may begin only when **all** hold:
- first-party traffic classification is active (Step 1 verified);
- MCP canary monitoring is active (Step 2);
- production persistence is stable on the selected backend (JSON now, or SQLite post-cutover, observed);
- credential hashing is activated **or** explicitly deferred with documented rationale;
- registry metadata is current and **independently indexed**;
- **one clean registry-led cold-discovery path succeeds end to end**.

## G. Phase transition

**Pilot A is closed and the architecture baseline is frozen at `a9d0380`.** Do not reopen Pilot A for ordinary Pilot B operational issues; reopen only if a **foundational defect** (a broken invariant in identity, attribution, persistence, credentials, reachability, or ledger integrity) is discovered. Pilot B proceeds through the operator-gated entry actions and the launch plan (`PILOT_B_LAUNCH_PLAN.md`).
