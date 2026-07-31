# Runbook — autonomous trust index, paid layer, experiment engine

Operating model: **no routine human involvement.** Nothing below is a daily
task. It is the list of switches, the order to pull them in, and how to undo
each one. If you are reading this because something is wrong, start at
§3 (Rollback) — every change here is reversible by configuration alone.

---

## 1. What runs, and when

One loop. The index does **not** get its own timer — it rides the existing
lease-guarded, jittered scout cycle (`app/swarm/runner.py`), so there is exactly
one place to look when outbound traffic misbehaves and exactly one kill switch.

Per cycle, in order:

1. `indexops.ingest` — fold source records into the index (dedupe by endpoint
   fingerprint, then identity).
2. `indexops.recheck_due` — probe the **stalest** entries first, capped at
   `GUILD_INDEX_RECHECK_BATCH`.
3. `_run_watch_cycles` — run due customer watches, charging **per cycle
   actually performed**.
4. `experiments.evaluate` — decide each running experiment, or refuse to.

A failure in any step is recorded in `swarm_state.last_run.index` and never
fails the cycle. Index upkeep must not be able to take the service down.

## 2. Switches

| Variable | Default | Effect |
|---|---|---|
| `GUILD_INDEX_AUTORUN` | `0` (prod `1`) | Master switch for **all** index upkeep |
| `GUILD_INDEX_INGEST` | `0` | Remote public-registry ingest. Separate on purpose |
| `GUILD_INDEX_FRESH_TTL_S` | `86400` | When an observation becomes stale |
| `GUILD_INDEX_RECHECK_BATCH` | `8` | **Outbound bound.** Endpoints probed per cycle |
| `GUILD_EXP_MIN_QUALIFIED` | `10` | Genuine-external actors before a verdict is allowed |
| `GUILD_EXP_WINDOW_DAYS` | `14` | Hard experiment window |
| `GUILD_PRICE_<OP>` | unset | Price override, clamped to the ceiling in `app/pricing.py` |

## 3. Rollback

Ordered least to most disruptive. **None of these require a deploy.**

1. **Stop index upkeep** — `GUILD_INDEX_AUTORUN=0`. The scout continues; the
   index freezes and serves its last observations, correctly labelled stale.
   Paid endpoints keep working on live probes.
2. **Stop outbound registry ingest only** — `GUILD_INDEX_INGEST=0`. Rechecks of
   endpoints already known continue.
3. **Throttle** — `GUILD_INDEX_RECHECK_BATCH=1`. Coverage slows; nothing breaks.
4. **Make a paid product free** — `GUILD_PRICE_DEEP_PREFLIGHT=0`. Metering stops
   charging immediately; nothing 402s.
5. **Full feature rollback** — revert the release. Every new surface is
   additive: no existing route, schema or price changed, and the new Store
   collections (`trust_index`, `watches`, `experiments`) are loaded with `{}`
   defaults, so an older build ignores them rather than failing to boot.

The persistent disk is untouched by a rollback — the new collections are kv
rows, not a schema migration.

## 4. Incident: "the index is probing too much"

Symptom: complaints from an operator, or outbound volume above expectation.

1. `GUILD_INDEX_AUTORUN=0` — stops everything immediately.
2. Confirm the bound was actually in force:
   `GET /diagnostics/state` for the serving instance, then read
   `swarm_state.last_run.index.recheck.capped_at`.
3. Every probe is SSRF-screened and bounded, sends no credentials, and
   identifies itself as `agent-guild-index/1.0` with a contact path. If an
   operator asks us to stop, add the endpoint's fingerprint to the index and
   set the entry `active: false` — do not argue the point.

## 5. Incident: "a customer was billed and got nothing"

This should be impossible by construction; verify rather than assume.

- **Evidence bundles** are produced *before* the meter runs. A refusal raises
  `409 evidence_issuance_refused` with `"billing": "NOT CHARGED"`.
- **Watch cycles** charge only after a successful observation. A cycle that
  could not observe returns `cycled: false` and is not billed.
- **Failed charge** suspends the watch (`active: false`,
  `suspended_reason: payment_failed`) rather than continuing to serve free.

Check `store.watches[<id>]` for `cycles_billed` versus `len(changes)` and the
`watch_cycle` entries in the billing log.

## 6. Incident: "an experiment killed something it should not have"

An experiment can only reach `kill` with `GUILD_EXP_MIN_QUALIFIED` **genuine
external** actors. Crawlers, first-party tooling and unknown-attributed traffic
are excluded structurally, so they cannot reach the threshold no matter how
much of them there is.

If a `kill` looks wrong, read `experiments[key].evidence.exposure` — it names
the rule and the actor count. To reverse: raise the price back with
`GUILD_PRICE_<OP>` (the engine only ever *halves* within the ceiling; it cannot
raise a price or invent an operation).

## 7. What to read, in order

`GET /commercial` — revenue first, then qualified exposure, then experiments,
then the supporting metrics explicitly labelled as unable to carry a decision.

The single number: **`external_settled_revenue_usd`** — independently confirmed
external mainnet settlement only. Sandbox credits, first-party canaries,
testnet funds and internal transfers are excluded by construction.

If that number is `0.00`, the correct reading is that **nothing has been sold
yet** — regardless of how large the index, the reach or the free-check count
is.

## 8. Safety invariants that must never be traded away

- A listing is never promoted to an observation.
- `unknown` is never promoted to `externally_owned`.
- Paid issuance fails closed; a partial evidence bundle is never emitted.
- No evidence page is published for an endpoint we have never called.
- Adapters read documented public APIs only, with a truthful User-Agent.
- We never transact with our own services to create activity, and first-party
  watches are excluded from `externally_monitored_endpoints`.
