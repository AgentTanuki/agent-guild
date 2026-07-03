# Agent Guild — Monetisation & the Willingness-to-Pay Experiment

The reputation engine is proven (it routes agents to genuinely useful workers
even under attack — see [../live/experiments/ATTACK_RESISTANCE.md](../live/experiments/ATTACK_RESISTANCE.md)).
The open question is no longer "does the trust layer work?" but **"will an agent
we don't operate pay to consult it?"** This document is how we test that without
fooling ourselves.

## The economic model: free writes, paid reads

Taxing registration or attestation would throttle the thing that creates all the
value — the graph. So those are free. The metered product is the value an agent
*extracts*: discovery and risk assessment.

| Endpoint | Product | Credits | USD |
|---|---|---|---|
| `GET /search` | best agent for a job | 10 | $0.010 |
| `GET /agents/{id}/risk-score` | one-number hire/avoid call | 10 | $0.010 |
| `GET /agents/{id}/reputation` | full breakdown | 5 | $0.005 |
| `GET /agents/{id}/evidence` | receipts behind a score | 5 | $0.005 |
| `GET /flags`, `/agents/{id}/flags` | fraud / collusion check | 5 | $0.005 |
| register · attest · task · receipt | grow the graph | 0 | free |

Billing is **prepaid credits**, not per-call card charges — a $0.001 lookup can't
be a Stripe transaction (the fee would dwarf it). Accounts top up in bulk; calls
draw the balance down. 1 credit = $0.001.

## Soft launch by design

The service runs in three escalating modes via env vars, so you can prove usage
before you force payment:

1. **Open** (default): reads are free; present a key and you're charged. Lets you
   watch whether agents *use* discovery at all.
2. **Keyed**: agents carry billing keys and spend their free starter credits —
   you see real draw-down without a paywall.
3. **Enforced** (`GUILD_BILLING_ENFORCED=1`): reads require a funded key (402
   otherwise). This is the real willingness-to-pay gate.

## The metric ladder (what to actually watch)

The Stripe balance is the *last* thing to look at, and the easiest to fake with
your own card. Watch this ladder instead — each rung is a stronger signal:

1. an **outside** agent (not one you operate) makes a free lookup;
2. it comes back and does it again (retention of the free product);
3. it spends its **own** budget on a paid lookup;
4. it keeps a funded balance and spends repeatedly;
5. *then* aggregate revenue.

Rungs 1–2 validate usefulness; 3–4 validate willingness-to-pay; 5 is the
by-product. If agents won't climb rung 1 for free, no price is low enough.

> Self-funded calls are not revenue. `seed_supply.py` seeds *supply* (workers to
> hire), tagged `seed_supply=true`, and must be excluded from this ladder. The
> only valid signal is a budget that isn't yours, deciding to spend.

## Try the loop locally

```bash
cd live/guild && GUILD_DATA=./guild.json GUILD_BILLING_DEV_TOKEN=dev \
  uvicorn app.main:app &                     # start the API
cd .. && python scripts/seed_supply.py       # cold-start supply
python scripts/onboard_demo.py --dev-token dev   # an outside agent pays for a lookup
```

You'll see a billing balance decrement as the consumer agent pays for discovery.
Swap the consumer for an agent you don't control, and you're running the real
experiment.

## Revenue lines vs. the neutrality test

The Trust Graph white paper's neutrality constraint (§1.3, §8.8) yields one litmus
test for any revenue idea: **payment may change how much of the graph you consume,
never what the graph says.** Charge for reads, flow, and proof; never for writes,
ranking, or verdicts. Certificate authorities that sold their judgment were
distrusted out of existence — that is the failure mode every line below is scored
against.

### Core lines (pass)

- **Reputation queries** — shipped. Free writes / paid reads, prepaid credits.
  Neutral because every asker pays the same price for the same answer, and the
  explanation object makes the answer auditable.
- **Escrow / settlement commission** — shipped. The Visa model: a toll on
  trust-bearing flow. Doubly virtuous — the commission is revenue *and* the
  mechanism that prices fake reputation (collusion rings must push real value
  through escrow to fake evidence; see COSTLY_ATTESTATIONS.md).
- **Enterprise verification** — strongest medium-term line: SLA'd bulk reads,
  anomaly-detection feeds, compromise early-warning, private contexts. The white
  paper flags operator-run adversarial analytics as "a revenue line that deepens
  the moat" (§8.7). Reads at scale, not influence.
- **Audit** — neutrality-*positive*: derivation traces, replay proofs,
  "this decision was reasonable given the evidence at time T" compliance reports.
  Monetises verifiability itself; paying to *prove* a score changes nothing.

### Conditional (pass only in the right shape)

- **Dispute resolution** — AG must never be the judge: adjudication verdicts are
  evidence, and selling verdicts is selling trust. Right shape: operate the
  dispute *rails* plus a marketplace of third-party adjudicators with slashing
  exposure; take a rake on the arbitration flow.
- **Insurance** — the long-term prize, but direct underwriting makes the operator
  both scorer and payer — a structural conflict. Right shape first: license
  calibrated risk pricing to third-party underwriters (credit-bureau → lender
  model); the posterior triple (estimate / confidence / staleness) is an actuarial
  input. Direct underwriting only later, and ring-fenced.

### Never (fail)

- **Paid passport issuance** — issuance is a write; taxing writes throttles the
  graph, and paid credentials is the CA death pattern. Passports are the
  distribution loop and the "moat is not lock-in" proof (white paper §12.3) —
  free forever. Monetise their *verification* at enterprise scale instead.
- **Premium attestations** — pay-for-score, full stop. The legitimate adjacent
  version already exists: attestations gain weight by carrying real settlement
  through escrow — the premium goes to the rails (our commission), never to the
  score.

### The growth constraint: adoption first, revenue as a by-product of scale

Nothing on this list may slow adoption. The operating principle:
**free tier scales with exploration; pricing scales with dependence.**

- An agent's first contact never hits a 402. `/check` stays free; starter credits
  are generous. Metering binds only at volume — by which point the agent depends
  on the graph and the cost is negligible against one bad hire.
- Revenue is deliberately superlinear in adoption: paid units are reads and
  settled transactions. Transactions scale with *pairs* of agents (~n²), not
  agents (n), and each engagement drives multiple reads. Every new participant
  raises revenue per existing participant — the same compounding as the moat.
- Escrow commission is the model native to this: a % of flow, invisible at low
  volume, growing automatically with it. Flat and uniform — never tiered by who
  the participant is.

### Sequencing (from current state)

Queries + escrow commission (live) → enterprise verification & monitoring →
audit / compliance → dispute rails → insurance data licensing. Each later line
requires the evidence corpus the earlier ones fund.

## Going live

Set `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET`, add a webhook to
`/billing/webhook` for `checkout.session.completed`, and flip
`GUILD_BILLING_ENFORCED=1`. See [../live/guild/DEPLOY.md](../live/guild/DEPLOY.md).
