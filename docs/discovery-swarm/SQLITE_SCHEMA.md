# SQLite runtime schema — complete collection mapping (GUILD_STORE=sqlite)

This is the **runtime** backend (`app/store_sqlite.py`, opt-in behind
`GUILD_STORE=sqlite`). The default JSON store is unchanged, byte for byte; only
the sqlite path is described here. (The offline migrate script,
`live/scripts/migrate_json_to_sqlite.py`, uses a separate, verification-oriented
schema — see its module docstring.)

Every write-sensitive table keeps the FULL original record in a `json` column
(nothing is ever lost); a few columns are lifted out and indexed for hot
lookups. Writes are **database-authoritative**: each write-sensitive operation
reads the affected CURRENT rows from SQLite inside one `BEGIN IMMEDIATE`
transaction and validates/computes against those rows, not a stale in-memory
snapshot (see "Write model" below).

## Complete Store-attribute -> table -> primary key / order mapping (all 16)

Every current `Store` collection is represented **exactly once**:

| # | Store attribute (`store.py`) | SQLite table | Primary key / order strategy |
|---|------------------------------|--------------|------------------------------|
| 1 | `agents` (dict)             | `agents`     | PK `id`. Lifted+indexed: `key_id`, `api_key_hash`, `revoked_at`, `expires_at`. `version` column. |
| 2 | `tasks` (dict)              | `tasks`      | PK `id`. `version` column. |
| 3 | `attestations` (list)       | `attestations` | PK `id`; load order `rowid` (insertion order). |
| 4 | `accounts` (dict)           | `accounts`   | PK `key` (billing key / key_id). Lifted: `owner_agent_id`, `balance`. `version` column. |
| 5 | `billing_log` (append-only list) | `billing_log` | PK `seq` **AUTOINCREMENT** — deterministic append order, one row per entry. |
| 6 | `events` (append-only list) | `events`    | PK `seq` **AUTOINCREMENT** — deterministic append order, one row per event (duplicates kept, mirroring the in-memory list). Indexed `(type, at)`. |
| 7 | `referrals` (list)          | `referrals`  | PK `referred_id` — one edge per referred agent (activation is an in-place upsert, not an append). |
| 8 | `health_log` (append-only list) | `health_log` | PK `seq` **AUTOINCREMENT** — one row per entry. |
| 9 | `identity` (dict, singleton) | `kv['identity']` | Whole-value blob keyed by name. |
| 10 | `ledger_records` (append-only, hash-chained) | `ledger` | PK `seq` = the record's **own** monotonic hash-chain sequence (NOT autoincrement), preserving chain order exactly. |
| 11 | `checkpoints` (append-only list) | `checkpoints` | PK `idx` = the checkpoint's own index. |
| 12 | `escrows` (dict)           | `escrows`    | PK `id`. Lifted+indexed: `status`, `requester_key`. `version` column. |
| 13 | `guild_revenue` (int, singleton) | **DERIVED** (SUM over `escrows` where `status=released`); `kv['guild_revenue']` kept only as a denormalised cache | Not a stored mutable counter — see "Derived revenue" below. |
| 14 | `demand_watches` (append-only list) | `demand_watches` | PK `seq` **AUTOINCREMENT** — one row per entry. |
| 15 | `swarm_state` (dict, singleton) | `kv['swarm_state']` | Whole-value blob keyed by name. |
| 16 | `outbound_invocations` (dict) | `outbound_invocations` | PK `id`. **Dedicated table** (not a kv blob): columns `agent_id`, `endpoint_fingerprint`, `status`, `started_at`, `completed_at`, `expires_at`, `json`, `version`. Indexed `agent_id`. |

Append-only collections (`events`, `billing_log`, `health_log`,
`demand_watches`, `ledger_records`) are one row per entry with deterministic
ordering — an `AUTOINCREMENT seq` for the instrumentation logs, and the
record's **own** sequence for the hash-chained `ledger`.

### `referrals` vs `collaborations` — the truth

The app has a `referrals` collection (agent->agent referral edges, keyed by
`referred_id`) and it is kept as `referrals` here. It is **NOT** renamed to
`collaborations`. There is no separate top-level `collaborations` Store
attribute; "collaboration records" are a *record type inside* `ledger_records`
(sealed onto the hash chain by `record_collaboration` / `append_task_to_ledger`)
— they are not a distinct collection, so nothing is conflated.

## Write model — database-authoritative (`BEGIN IMMEDIATE` authoritative read)

Under `GUILD_STORE=sqlite`, a whole mutating method runs inside one
`BEGIN IMMEDIATE` transaction (`Store._txn()`), and the write-sensitive
operations first refresh the affected rows from SQLite (`_sync_agent_from_db`,
`_sync_account_from_db`, `_sync_escrow_from_db`, `_sync_task_from_db`,
`_ledger_head`, `backend.fetch_kv`, `backend.fetch_invocation`) so permission /
status / balance / sequence checks run against the **current committed** rows.
Because `BEGIN IMMEDIATE` takes the reserved write lock up front, no other
writer interleaves between the authoritative read and the commit — the
read-modify-write is serialized, not merely locked in-process.

Operations made database-authoritative this way: credit/debit (`charge`,
`credit`, `grant_trial`), escrow `open`/`release`/`refund`/`dispute` (including
the DERIVED `guild_revenue`; see "Derived revenue"), credential `rotate`/`revoke`, endpoint replacement
(`set_agent_endpoint`), outbound invocation begin/complete, receipt acceptance
(`submit_receipt`), ledger appends (`append_ledger_event`,
`append_task_to_ledger`) and checkpoint publication.

**Optimistic concurrency.** `agents`, `accounts`, `tasks`, `escrows` and
`outbound_invocations` carry an integer `version` column. Every `put_*` is a
version-bumping UPSERT (`ON CONFLICT ... version=version+1`). A compare-and-swap
path (`update_agent_cas(rec, expected_version)`) applies a write only if the
stored version still matches, returning `False` on a stale write so a caller
retries/fails rather than silently clobbering newer state (proven by
`test_stale_version_write_is_rejected`). Most operations rely on the
authoritative `BEGIN IMMEDIATE` read+write; the version column + CAS are the
explicit lost-update guard on top.

## Derived revenue (amendment 1 — no read-modify-write counter)

`guild_revenue` is **not** a mutable counter under sqlite. It is **DERIVED** from
the committed settlement records as a query:

```sql
SELECT COALESCE(SUM(json_extract(json,'$.fee')),0)
FROM escrows WHERE status='released';
```

exposed as `SqliteBackend.guild_revenue_total()`. `release_escrow` settles the
escrow (`status='released'`, committed in the same `BEGIN IMMEDIATE`) and then
sets the in-memory `self.guild_revenue` cache from that query; a fresh
`Store.load` re-derives it the same way. Nothing ever does
`revenue = read(); revenue += fee; write(revenue)`.

**Why the escrows table, not the ledger.** The amendment's preferred source was
the ledger's `escrow_event/released` rows (which also carry the fee). We chose
the **escrows** table instead, for two reasons: (1) it is idempotent by
`escrow_id`, the table's PRIMARY KEY — a given escrow contributes its fee **at
most once** no matter how many times a release is attempted, because release is
guarded by an authoritative `status='funded'` read, so a second attempt is
rejected before it can add a second `released` row anywhere; and (2) it is
**complete regardless of when ledger dual-write began** — every settled escrow
is in the escrows table by construction, whereas very old settlements predating
the ledger dual-write would be missing from the ledger and under-count a
ledger-derived figure. The result is a value that is ALWAYS exact and that
concurrent releases can neither clobber nor double-count (proven under
multi-process contention by `test_concurrent_escrow_releases_guild_revenue_exact`
and `test_concurrent_distinct_escrow_releases_revenue_exact`, and under retry by
`test_revenue_increment_not_duplicated`).

## Retry idempotency (amendment 3 — a retry commits exactly once or fails)

The backend retries on `SQLITE_BUSY`/lock conflict. It **cannot** duplicate a
committed side effect, and this is proven, not asserted:

- The retry boundary is a **single SQL statement** — `BEGIN IMMEDIATE`
  (`_begin`), a `put_*`/append (`_exec`), or `COMMIT` (`_commit`) — **never a
  replay of the Python method body**, so no in-Python mutation is ever
  re-executed. (The current design already had this shape: the retry does NOT
  wrap the whole transaction, so there was no in-Python replay to fix; the
  hardening was to make revenue derived and to document + test the guarantee.)
- SQLite guarantees a statement that returns `SQLITE_BUSY` **did not modify the
  database** (the lock is refused before any page write), so re-issuing it can
  never double-apply. `BEGIN IMMEDIATE` only fails BUSY while acquiring the write
  lock (nothing applied yet); `COMMIT` is atomic (a BUSY'd commit left the txn
  open → the retry commits it once; an already-succeeded commit closed the txn →
  a spurious retry is a no-op).
- Every effect is idempotent by a **natural key** on replay: `agents.id`,
  `accounts.key`, `tasks.id`, `escrows.id`, `outbound_invocations.id`,
  `ledger.seq` (INSERT OR REPLACE), rotation on the single agent row keyed by the
  new `key_id`, and revenue as the derived SUM above.

`tests/test_sqlite_retry_idempotency.py` injects `OperationalError("database is
locked")` once or twice *before* the real statement runs (modelling
BUSY-before-apply) at both the `BEGIN` and `COMMIT` boundaries, and asserts NO
duplication of any of the seven side-effect types: **events, ledger records,
billing entries, receipts, escrow movements, revenue increments, and
credential-rotation events**.

## Event-persistence decision (JSONL under sqlite)

Under `GUILD_STORE=sqlite`, **SQLite is the canonical event store**. The
`.events.jsonl` sidecar journal is **NOT** a second required source:

- `record_event` writes the event row inside the transaction (`_persist_event`)
  and does **not** append to JSONL under sqlite.
- `_journal_event` is a no-op when a backend is configured (`self.backend is not
  None`), so no post-commit JSONL append can happen — this removes the crash
  window between the SQLite commit and a separate JSONL write that could
  otherwise create an inconsistent claim, and avoids double-appends.
- The JSONL journal is read **exactly once**, at first-boot cutover from a
  pre-sqlite JSON store (`_load_sqlite` -> `_replay_event_journal` while the DB
  is empty), to import legacy events into the `events` table. On the normal
  sqlite load path `_replay_event_journal` is not called, so events are never
  double-counted.

Rollback to the JSON store re-enables the journal automatically (the guard keys
off `self.backend`).
