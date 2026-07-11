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
backend. Two invariants in this backend are made safe by the database-authoritative
`BEGIN IMMEDIATE` read+write (proven under multi-process contention):
ledger-chain `seq` under concurrent appenders and account-rekey orphan rows
under concurrent same-agent rotation. `guild_revenue` is no longer a mutable
counter at all — it is DERIVED as a SUM over settled escrows (idempotent by
`escrow_id`), so it can never be clobbered or double-counted regardless of
writer count (see `SQLITE_SCHEMA.md` → "Derived revenue").

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
lost-update guard (see `docs/discovery-swarm/SQLITE_SCHEMA.md`). The invariants
previously flagged single-writer-only — ledger-chain `seq`, account-rekey
orphan rows, and (formerly) the `guild_revenue` counter — are now exercised
UNDER MULTI-PROCESS in `tests/test_sqlite_backend.py` and hold (contiguous seq,
zero orphan account rows, and an exact, now-DERIVED guild_revenue). They are correct at any process
count on ONE disk; the single-worker rule below is about avoiding SQLite's
`SQLITE_BUSY`/lock contention and the fact that a disk cannot be shared across
instances, NOT about those invariants being unsafe.

## Startup guard — read as THREE distinct layers (do not conflate them)
The startup guard does NOT, and cannot, guarantee "single instance." Its
protection and the infrastructure constraint are separate things:

1. **Application guard = worker-PROCESS protection ONLY.** `Store.__init__`
   refuses to start when `GUILD_STORE=sqlite` AND this container is knowingly
   configured for more than one writer PROCESS — `WEB_CONCURRENCY`,
   `GUILD_WORKERS` or `UVICORN_WORKERS` `> 1`, or an explicit `uvicorn --workers
   N`. That is the whole of what the code can observe. It CANNOT see how many
   Render service INSTANCES are running; a second instance running one worker
   each would each pass this guard. It raises a fatal `RuntimeError` rather than
   silently running multiple writers. Single-node override (accepting the risk):
   `GUILD_SQLITE_ALLOW_MULTIWORKER=1`.

2. **Render persistent-disk topology = the single-INSTANCE constraint.** What
   actually keeps writers to one is infrastructure, not the app: a Render
   persistent disk **cannot be mounted by more than one instance**, so while the
   SQLite file lives on that disk the service cannot be horizontally scaled. The
   application **cannot verify this from inside the process** — it is an
   operational invariant enforced by Render that the app trusts but does not
   check. The guard does NOT prove it.

3. **Any future topology change REQUIRES a Postgres migration review FIRST.**
   Adding instances, enabling autoscaling, removing or detaching the persistent
   disk, or moving off Render — anything that could admit a second writer — MUST
   go through an explicit Postgres migration review BEFORE the change is made.
   SQLite on a shared or absent disk re-introduces the whole-file clobber class
   of failure, and neither the guard (layer 1) nor the disk constraint (layer 2)
   will catch it once the disk is no longer the single mount point.

**In one line:** the guard guarantees single-PROCESS-per-container, NOT
single-instance; single-instance is a Render-disk property; changing the
topology is a Postgres-migration decision, not a config toggle.

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
