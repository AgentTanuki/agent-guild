# Operating mandate — from 2026-08-01

GOVERNING for the daily scheduled operations pass. Supersedes the passport-count
framing of `docs/AUTONOMOUS_PIVOT_2026-07-31.md` for **metric selection only**;
every honesty and no-human-selling constraint in that document still stands.

## Why the metric changed

Passport counts stopped being able to carry a decision. On 2026-08-01 the funnel
read: 3 cumulative "external passports issued", of which `subject_self_claim`
external = **0** — all three were third parties fetching someone else's
credential. Meanwhile `/commercial` showed $0 confirmed external revenue, 0
payers, 0 paid decisions, 0 external watches and **0 qualified paid-offer
impressions**. Index coverage rose 48 → 80 of 93, so inventory collection was
working and was not the constraint.

A number that has not moved in a month, and that overstates what it measures,
is not an operating metric. It is a comfort.

## The numbers, in order

1. **PRIMARY — independently confirmed external mainnet revenue.**
   USD settled on Base mainnet by a counterparty that is not us. First-party
   canaries, sandbox credits, testnet and circular transfers are **not** revenue
   and may never be reported as such.
2. **LEADING — qualified paid-offer impressions, split by operation and by
   source** (`GET /funnel/paid`). Distinct qualified external actors shown a
   paid offer. Raw impressions are reported beside it and are reach, not
   attention.

   **QUALIFIED, precisely** (integrity correction, 2026-08-01). An impression
   qualifies only if the caller is:

   * an authenticated registered member (`EXTERNAL_MEMBER` /
     `EXTERNAL_VERIFIED`) — authentication is identity, so a bare user agent is
     fine; or
   * an `EXTERNAL_UNKNOWN` that `attribution.is_genuine_external` accepts: a
     named MCP client we do not operate, or a recognised agent-framework user
     agent.

   Bare `curl`/`wget`/`urllib`/`requests`, empty user agents, `mcp/remote`,
   crawlers and unrecognised tooling **never qualify**. They are
   indistinguishable from our own traffic — that is not a guess, it is what
   `is_genuine_external` has said since 2026-07-10. **A stable IP+UA actor
   proves DISTINCTNESS, not external agent intent.**

   This was got wrong on first release: `/funnel/paid` gated on
   `may_count_as_external_growth`, which passes `EXTERNAL_UNKNOWN`. The
   deployed 2.0.3 readback then reported qualified actors that were, without
   exception, ours or a probe — verification `curl`s, `guild-live-conformance`
   (our own release gate), `agent-guild-scout` (our own scout, arriving over
   the network so the in-process origin stamp does not apply) and an A2A
   registry health check. Zero external demand, reported as `measurable: true`.
   Raw impressions were and remain correct; only the qualified denominator was
   wrong.
3. **SUPPORTING ONLY — passport counts, registrations, index coverage, raw
   traffic.** These may inform, never decide, and never lead a report.

## Rules

* **One bounded autonomous ops cycle per scheduled run**, and only after
  verifying the deployed SHA and a stable baseline. Verify first, act second.
* **One reversible acquisition change per cycle**, with a predeclared success
  criterion and a predeclared rollback. Never two copy/channel/price changes at
  once — simultaneous changes cannot be attributed and therefore teach nothing.
* **No price change until a predeclared adequate denominator of qualified
  impressions exists.** At zero impressions the price is not the variable under
  test; exposure is. `/funnel/paid` reports `measurable: false` rather than a 0%
  conversion rate, and a zero denominator is reported as *not measurable*, never
  as failure.
* **Inventory is maintenance.** Finish the remaining 13 index observations, then
  only bounded, due/risk-prioritised rechecks. Effort moves from catalog growth
  to distribution.
* **Escalate to Ross** only for: service unreachable, release regression, ledger
  or chain failure, admin token unavailable, event loss, registry listing lost,
  trust/attribution corruption, or a genuine regression in the primary or
  leading metric. A first sale is a milestone to report prominently — not an
  escalation.

## Verified constraint: the x402 Bazaar is not reachable yet

Checked against primary sources and the live catalog on 2026-08-01:

* CDP `/discovery/search` returns **0 entries** for this host and 0 for each of
  `deep_preflight`, `evidence_bundle`, `watch_cycle`.
* There is **no protocol-native publication or registration call** — not in the
  x402 v2 spec, not in the bazaar extension spec, not in CDP's facilitator API
  (which exposes exactly two write endpoints: `verify` and `settle`).
* Listing is a **side effect of a settled payment** through the CDP facilitator,
  on a route whose 402 carried a conformant `bazaar` extension **that the paying
  client echoed back**. The echo is the buyer's choice, not ours.

Therefore Bazaar presence is **downstream of a first sale, not upstream of it**,
and cannot be an acquisition channel at $0 revenue. Do not re-litigate this
without new primary evidence. A 402 challenge containing a bazaar extension is
**not** proof of catalog presence; only a catalog query is.

The next strongest automated machine-to-machine discovery surfaces are the ones
we serve ourselves — A2A agent card, MCP tool discovery,
`/.well-known/agent-guild.json`, `/llms.txt`, and the MCP Registry listing.
Each carries the paid catalog with a stable source id (`app/paidcatalog.py`),
so "which surface produces qualified paid attention" is measured per source.

### Observable click-through vs unobservable listing views

The MCP Registry listing is the one already-live EXTERNAL surface, and it is a
special case that must not be reported loosely.

* **Unobservable.** A machine reading our entry in the registry's own API or
  UI is invisible to us. We serve nothing, we log nothing, and no number we
  hold describes it. Registry impressions of that kind are **never counted**
  and must never be estimated, inferred from registry traffic, or presented as
  reach.
* **Observable.** The listing's `ai.agent-guild/paid-operations` block names one
  callable catalog URL,
  `GET /.well-known/agent-guild.json?src=paid_offer:registry`. A machine that
  FOLLOWS it hits our service, and that click-through is recorded as a
  `paid_offer:registry` impression like any other.

So `by_source["paid_offer:registry"]` in `/funnel/paid` means *machines that
acted on the listing*, not *machines that saw it*. The first is the number; the
second is unknowable and stays that way. `src` is a closed allowlist — an
unrecognised value is ignored, never recorded as claimed — so the source cannot
be forged by a caller.

The listing carries **no prices**. Prices move when the experiment engine runs
and a registry entry is republished rarely, so a copied price would be stale,
and a stale price is a lie. Names go in the listing; numbers live at the
catalog URL.

## Standing prohibitions

No manual outreach. No unsolicited human messaging. No fake actors, no
self-payments, no circular transfers, no testnet presented as revenue, no
first-party presented as adoption, no crawler volume presented as demand. Do not
enable spend. Do not require a user credential. Payment and release gates stay
fail-closed.
