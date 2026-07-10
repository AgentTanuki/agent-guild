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
