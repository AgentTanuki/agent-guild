# Branch notes — sqlite-persistence

Concern (ONE): persistence-layer cutover. Nothing else.

## Owns (may modify)
- live/guild/app/store.py — ONLY the persistence engine: __init__ backend
  selection, _load / _save / _journal_event, and a new SQLite+WAL backend
  behind GUILD_STORE=sqlite (default json = unchanged). MUST NOT change
  credential/auth logic, keying semantics, set_agent_endpoint, or reachability.
- live/scripts/migrate_json_to_sqlite.py (already on main; may extend).
- Tests: tests/test_sqlite_backend.py (new). (test_sqlite_migration.py already
  on main — may extend, not rewrite.)
- docs/discovery-swarm/PERSISTENCE_MIGRATION.md (append cutover section).

## Must NOT touch
- credentials.py, _require_key, derive_actor auth, scopes -> credential-hardening
- reachability.py, set_agent_endpoint, declare_endpoint -> reachability-verifier
- generated state / production data / secrets / evidence files

## Flag / safety
- GUILD_STORE default "json" = current behavior. SQLite path is opt-in and
  never migrates production data automatically. Production cutover is a
  separate operator step: backup guild.json -> run migrate script --verify ->
  set GUILD_STORE=sqlite -> deploy -> before/after probe. NOT part of merge.

## Cross-branch dependency
- store.py is the HOT file (all three branches touch it, different regions).
  This branch integrates LAST so it is rebuilt against the final store shape
  after credential + reachability regions have landed.

## Status
- New branch from main 2f5b60a. Migration script + test already on main.
  Cutover backend not yet built.

## Status (built 2026-07-10)
- SqliteBackend (app/store_sqlite.py) + GUILD_STORE wiring + write-through hooks + transactional invariants.
- 393 pass in all 3 modes (json / sqlite / sqlite+hashing). 13 real multi-process concurrency tests.
- Concurrent registrations: JSON loses 74%, SQLite loses 0.
- Deployment decision: docs/discovery-swarm/SQLITE_CUTOVER.md. Single-instance/single-worker only; multi-instance -> Postgres.

## Status (database-authoritative amendment, 2026-07-11)
- Writes under GUILD_STORE=sqlite are now DATABASE-AUTHORITATIVE (BEGIN IMMEDIATE
  authoritative read + validate + write in one txn), not in-memory RMW-then-persist.
- version columns (agents/accounts/tasks/escrows/outbound_invocations) +
  update_agent_cas CAS (stale-write rejected test).
- outbound_invocations promoted to a DEDICATED table (was a kv blob).
- SQLite is the canonical event store; .events.jsonl disabled under sqlite.
- Startup guard: refuse GUILD_STORE=sqlite with >1 configured worker.
- The 3 previously single-writer-only invariants (ledger seq / account rekey /
  guild_revenue) now pass UNDER MULTI-PROCESS.
- Complete 16-collection schema map: docs/discovery-swarm/SQLITE_SCHEMA.md.
- Migration report: honest raw-key auth (only with a raw key / --test-key) + all
  16 collections compared.
- 397 pass in all 3 modes (json / sqlite / sqlite+hashing); 17 multi-process tests.
- NB: this amendment intentionally touches set_agent_endpoint/rotate/revoke to add
  authoritative reads (per the task); credential/keying SEMANTICS are unchanged.
