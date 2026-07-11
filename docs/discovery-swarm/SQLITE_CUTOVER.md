# SQLite backend — deployment decision & cutover (2026-07-10)

Backend built on branch `sqlite-persistence`, gated behind `GUILD_STORE`
(default `json` = current behavior, untouched). Deploy the code DARK first;
do NOT set `GUILD_STORE=sqlite` in production until the checklist below passes.

## Confirmed Render topology
- Service `agent-guild`, `plan: starter`, with a **persistent disk** (1 GB at `/data`).
  A Render disk **cannot be mounted by more than one instance**, so the service
  **cannot be horizontally scaled** → single instance is guaranteed while a disk
  is attached.
- Container CMD runs `uvicorn app.main:app` with **no `--workers`** → **one
  worker process**.
- Therefore production is **single-instance, single-worker**. Multi-PROCESS
  contention (the JSON store's 74%-write-loss failure) does not occur in prod;
  it is exercised only by the test suite.
- Backup/restore: the JSON state file + its `.events.jsonl` journal live on the
  `/data` disk. Backup = copy those files (or a Render disk snapshot); restore =
  put them back. The SQLite DB would live on the same disk.
- Deploy behavior: Render stop-then-starts the single instance on deploy; the
  disk persists across deploys/restarts.

## What SQLite adds at this topology
Crash **durability** (WAL + `synchronous=NORMAL` survive a hard kill mid-write
better than a non-fsync whole-file `os.replace`), real **transaction
boundaries** for multi-entity invariants, per-entity writes (no whole-file
rewrite per event), and headroom if a worker is ever added. It is NOT required
for correctness today: the JSON store already replaces the whole state
atomically per operation within the single process.

## What SQLite does NOT solve — and when to choose Postgres
SQLite on a local disk is still single-node. If AG ever needs **multiple
instances**, a Render disk cannot be shared, so SQLite is not an option and the
answer is **managed Postgres** with a proper relational model — not this
backend. Three invariants in this backend are correct only under a single
writer (documented, and non-scenarios at the confirmed topology):
ledger-chain `seq` under concurrent appenders, account-rekey orphan rows under
concurrent same-agent rotation, and the `guild_revenue` global counter under
concurrent escrow releases. Under a single writer none can occur.

## Suitability
**SQLite is suitable ONLY for ONE Render instance + ONE mounted disk + ONE
uvicorn worker.** Horizontal scale or multiple instances **REQUIRES migration to
Postgres** — a Render disk cannot be shared, and multiple SQLite writers on a
shared file re-introduce the whole-file clobber class of failure. At the
confirmed single-instance/single-worker topology it is suitable for Pilot B.

### Database-authoritative writes (why the "single-writer-only" invariants now hold)
Writes under `GUILD_STORE=sqlite` are database-authoritative: each
write-sensitive op reads the current rows from SQLite inside one
`BEGIN IMMEDIATE` transaction and validates/computes against those (not a stale
in-memory snapshot), with a `version` column + compare-and-swap as the
lost-update guard (see `docs/discovery-swarm/SQLITE_SCHEMA.md`). The three
invariants previously flagged single-writer-only — ledger-chain `seq`,
account-rekey orphan rows, and the `guild_revenue` counter — are now exercised
UNDER MULTI-PROCESS in `tests/test_sqlite_backend.py` and hold (contiguous seq,
zero orphan account rows, exact guild_revenue). They are correct at any process
count on ONE disk; the single-worker rule below is about avoiding SQLite's
`SQLITE_BUSY`/lock contention and the fact that a disk cannot be shared across
instances, NOT about those invariants being unsafe.

## Startup guard (fail fast on a multi-worker misconfiguration)
`Store.__init__` refuses to start when `GUILD_STORE=sqlite` AND the process is
knowingly configured for multiple workers — `WEB_CONCURRENCY`, `GUILD_WORKERS`
or `UVICORN_WORKERS` `> 1`, or an explicit `uvicorn --workers N` on the command
line. It raises a fatal `RuntimeError` explaining SQLite is single-writer-only
and pointing at Postgres for horizontal scale, rather than silently running
multiple writers. Single-node override (accepting the risk):
`GUILD_SQLITE_ALLOW_MULTIWORKER=1`.

## Cutover checklist (all must be YES before `GUILD_STORE=sqlite`)
1. Topology still single-instance + single-worker (disk attached, no `--workers`; the startup guard enforces this). ✅ confirmed 2026-07-10.
2. Backup taken: copy `/data/guild.json` + `/data/guild.json.events.jsonl`. ☐
3. Run the migration to a NEW file on the disk (source untouched):
   `python scripts/migrate_json_to_sqlite.py --data /data/guild.json --out /data/guild.db` → RESULT: verified OK. ☐
4. Choose a quiet cutover window (deploy stop-then-starts the instance). ☐
5. Set `GUILD_STORE=sqlite` + `GUILD_DATA=/data/guild.db`, deploy, verify `/health` + a probe. ☐

## Rollback
The migration never modifies the JSON source. To roll back, unset
`GUILD_STORE` (or set `json`) and redeploy — the JSON store resumes from
`/data/guild.json`. **Caveat:** any writes that landed in SQLite AFTER cutover
are NOT in the JSON file. If rollback is needed after live writes, either
(a) accept the loss of the post-cutover window, or (b) re-export SQLite→JSON
first (a reverse-migration script would be needed; not built — flagged risk).
Mitigation: keep the cutover window short and low-traffic, and re-migrate
forward rather than rolling back if possible.
