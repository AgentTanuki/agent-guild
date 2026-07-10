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
**SQLite is suitable for Pilot B at the current single-instance/single-worker
topology.** It is NOT suitable beyond that (multi-instance) — that requires
Postgres.

## Cutover checklist (all must be YES before `GUILD_STORE=sqlite`)
1. Topology still single-instance + single-worker (disk attached, no `--workers`). ✅ confirmed 2026-07-10.
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
