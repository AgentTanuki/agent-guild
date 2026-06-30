# Sprint — The Ledger's Write Path: one-call verifiable collaborations (2026-06-30)

## Loop: observe → find the limiting factor → remove it

**Observe.** The moat direction is locked (canonical ledger of verifiable AI-to-AI
collaboration) and the reference architecture ships. But the ledger holds **zero
real collaborations** — its records were all bootstrap. Four sprints of supply-side
work; no real interaction has entered the system.

**Limiting factor.** I had proposed challenge-resolution governance next — but that
is premature: *you cannot challenge records that don't exist.* The binding
constraint on the chosen moat is upstream: **there was no low-friction way for a
real, verifiable collaboration to enter the ledger.** Recording one took four
chained calls (register → task → receipt → attest); no real agent workflow does
that. Until removed, the ledger cannot accumulate the data that *is* the moat. So I
reordered (using granted authority) to remove this first.

**Removed it.** A one-call recording primitive that produces a single
highest-provenance (`guild_mediated`) ledger entry.

## Built (additive, reversible — not the irreversible stage-3 migration)

| # | Change | Files |
|---|--------|-------|
| 1 | **`Store.record_collaboration()`** — atomically creates the task, content-addresses the deliverable (sha256), stores the graded receipt, and writes the requester's receipt-backed attestation → one `guild_mediated` record. Plus `ledger_record_for_task()` to return the sealed, projected VCR. | `live/guild/app/store.py` |
| 2 | **`POST /collaborations`** (X-API-Key auth) + **`guild_record` MCP tool** — the one-call write path on both transports; records a `delegation` instrumentation event. | `live/guild/app/main.py`, `mcp_server.py`, `models.py` |
| 3 | **Discovery surfaces** — manifest `record_collaboration` endpoint, llms.txt, README tool table + MCP tool list. | `main.py`, `README.md` |
| 4 | **Tests** — one call yields a `guild_mediated`, content-addressed record; it appears in the ledger and keeps the chain valid; auth required; self-collaboration and bad outcomes rejected. | `tests/test_collaboration.py` |

**Verified end-to-end** (fresh external agents, no bootstrap): register → one
`POST /collaborations` → `guild_mediated` record `vcr_…` at chain seq 0, deliverable
content-addressed, ledger `chain_valid`, derived reputation updated, Guild-signed
checkpoint commits to it. Full suite **75 passed** (4 new).

## Why this was the right limiting factor to remove
- **Moat:** it's the write path that lets the canonical ledger accumulate real,
  verifiable, provenance-tagged collaborations — the un-back-fillable asset.
- **Network effect:** every recorded collaboration deepens the ledger and improves
  the reputation everyone derives from it. One call = one contribution.
- **Unblocks the rest:** challenge-resolution, dual-write persistence, and
  risk-pricing all presuppose records exist. Now they can.

## Next limiting factor (ranked)
1. **Persisted dual-write (stage 2):** append sealed VCRs to durable state on each
   `/collaborations` (today the ledger is a deterministic projection). Additive;
   makes the chain durable and checkpoints meaningful over time.
2. **Challenge-resolution + stake/slashing governance:** now that records can
   exist, make them contestable — the credibility layer.
3. **Distribution volume** remains the ultimate gate and is partly Ross-gated
   (AgentTanuki posting, awesome-mcp-servers PR #8585, agent-guild.ai). But every
   agent that now shows up can contribute a verifiable ledger record in one call.
