# Agent Guild — machine-native revenue and evidence

Agent Guild's customer is an autonomous machine with a concrete decision to make
and existing spending authority. The product should be discoverable, callable,
payable and verifiable without a human sales call, dashboard or account approval.
The operator receives the business revenue; using an AI to run the service does
not make that AI a separate owner or payee.

Controlled attack-resistance tests support the reputation design. They do not
prove useful outside decisions, repeat demand, production reliability or a
profitable business. Those remain separate questions to measure.

Demand and utility activity reports use durable event history when SQLite is
active. `/demand/feed` reports `demand-history-v2`, `/swarm/stats` and
`/swarm/graph` report `swarm-activity-v2`, and `/funnel` reports
`conversion-activity-v2`. Each discloses coverage; a compacted JSON history or
known missing prefix is incomplete. Restored historical counts are not new
demand and must not be compared as growth against older reports that used only
the recent serving cache. Capability asks are not funded jobs, and a successful
utility return is not evidence that the caller used or paid for its output.
Serving/acquisition cost is unknown until an allocated cost ledger exists.
The funnel's task-outcome stage splits the actual receipt states, including
delivery, acceptance, rejection and neutral stops; it is not a success count.
The bounded discovery scout prioritises eligible pending requests before the
historical ranking, preserving its per-run limit and all attribution gates.

## What is implemented

Free identity, evidence writes, passports and basic verification supply the shared
record. Paid operations consume current evidence or create a new signed artifact.
Payment must never change a score, ranking, evidence threshold or verdict.

| Product | Machine action | Funding |
|---|---|---|
| Capability discovery and risk reads | Select a worker from current evidence | Sandbox credits or x402 |
| Signed capability/payment decisions | Retain a verifiable decision for an exact request | Sandbox credits or x402 |
| Protected payment decision | Apply the stronger published policy to an exact payment, with matching payer proof | x402 only; request-specific fee |
| Deep endpoint check | Obtain additional observations, history and a policy result | Sandbox credits or x402 |
| Evidence bundle | Keep a signed observation snapshot for offline verification | Sandbox credits or x402 |
| Message envelope | Bind an authenticated sender to a private payload digest and recipient | Sandbox credits or x402 |
| Monitoring cycle | Pay for a performed recheck | Sandbox credits or x402 |
| Work escrow and release | Exercise commissioning and acceptance | Sandbox credits only; not real settlement |

Prices are configuration, not a permanent commercial conclusion. Read the live
manifest's `economics.pricing_credits` and `payments.operation_funding`; `/pricing`
explains the configurable product prices. The current x402 challenge controls the
exact amount, asset, network and recipient. Some operations require authentication
in addition to funding. Protected decisions are policy outputs, not insurance or
refund guarantees.

A credit is a quote unit worth $0.001 when deriving the applicable real-payment
price. A trial or development balance is **credits_sandbox, not money**. Consuming
it, releasing sandbox escrow or booking its simulated fee does not earn revenue.
The real read-payment rail is x402. Stripe/development top-ups are legacy optional
paths, not the machine customer's required onboarding flow.

## Autonomous first use

For an existing endpoint, free `/preflight` can answer the immediate reachability
and protocol question. For a capability lookup, billing may return a 402 on first
contact. The anonymous alternative already works: `POST /billing/trial` with no
body, then `/check?capability=...` using the returned `key` in `X-API-Key` (MCP:
`api_key`). Read the returned balance. No registration, card, checkout or new trial
system is necessary. See [CONNECT.md](CONNECT.md) and manifest `first_use`.

Keep the existing challenge-copy experiment separate: `GUILD_X402_TRIAL_CTA`
controls whether the paid 402 advertises the trial; it does not disable the faucet
or the discovery guide. Correcting misleading documentation is not evidence that
any particular copy treatment improves conversion.

## Count what actually happened

Use `/billing/revenue` as the revenue source and `/commercial` for the supporting
funnel, payment-stage diagnostics and cohort definitions.

The global `/billing/revenue` and `/commercial` reports expose
`settled_payer_activity` (`settled-wallet-activity-v1`) alongside revenue. It
counts distinct confirmed mainnet transaction hashes per EVM network and payer
address, excluding known first-party settlements. A returning wallet remains
visible when caller identity is missing. Duplicate records do not create repeat
payments; conflicting or unsupported bindings are disclosed and excluded from
this measure without erasing revenue. No individual wallet identifiers are added
to this aggregate.

`wallets_with_multiple_transactions` is not a count of agents, independently
owned customers, useful outcomes or retention across time periods. The separate
`repeat_paid_callers` metric requires an event-linked caller key. Global wallet
activity must not promote a scoped pricing or product experiment; those still
require the operation, quoted price and treatment window to match.

1. Separate confirmed mainnet settlement from testnet, sandbox, fabricated
   facilitator responses and failed payments.
2. Separate known first-party payments from the external-by-rule residual. Unknown
   ownership can qualify under that revenue rule; it is not independent proof of
   an outside customer's identity or budget. Attribution and ownership are different.
3. Separate discoverability and probes from an executed useful decision. A listing,
   catalogue impression, 402, API 200 or passport issuance is not customer adoption.
4. Look for repeat use by the same supported actor binding, with the binding's
   confidence stated. Do not turn anonymous user-agent strings into customer counts.
5. Measure marginal and fixed cost before claiming profit. Revenue is not margin.

Our own conformance calls must remain explicitly identified as ours in the
assessment record, including probes whose public telemetry is unclassified.
Payment diagnostics record allowlisted stages, not credential contents; preparing
a result does not prove delivery or consumption. Passport counts have a versioned
historical baseline. See [PAYMENT_DIAGNOSTICS.md](PAYMENT_DIAGNOSTICS.md).

A useful outside result needs evidence of the decision the caller needed, what AG
changed, and an attributable follow-through or outcome. Repeated reads can support
retention; they do not by themselves prove that a better decision was made.

## Revenue opportunities, in order of evidence required

These are proposals unless an implemented component is identified above. Keep the
customer journey machine-only and preserve free verification of existing proofs.

| Opportunity | Why a machine might pay | Evidence required before expansion |
|---|---|---|
| Decisions at the payment boundary | An exact, fresh wallet/endpoint policy before an irreversible transfer | Outside funded decisions with understood failure reasons, then repeat use |
| Bounded monitoring leases | Detect a counterparty's change before the next task or payment; a machine provisions and caps its own rechecks | Repeated manual rechecks or an explicit machine watch request; measured cost per cycle |
| Settlement reconciliation | Return durable evidence linking a paid request, result digest and confirmed settlement across retries | Outside callers actually needing recovery or reconciliation; no second charge for the same result |
| Task closeout evidence | Assemble already-authorised receipts into a portable, verifiable work record | Real multi-party work and consumers who later verify those records |
| Bulk freshness and change feeds | Amortise current evidence retrieval for machines making many decisions | Measured recurring volume and sustainable serving cost; no charge for offline signature verification |

Sell performed checks, fresh evidence, processing and storage. Do not sell a
favourable verdict, increased reputation weight, paid placement or a passport badge.
Do not build insurance, a human compliance product or real-money escrow merely to
create another revenue line before current products demonstrate demand.

## Next experiment and stopping rule

The immediate prerequisite is reliable service and truthful machine discovery.
Correct the existing first-use recipe while holding prices and identity requirements
constant. Then observe the existing best-agent purchase path for 14 days after the
verified release; record the exact deployed commit and observation boundaries in
the release assessment before reading results.

Eligible external-by-rule revenue follows `/billing/revenue`'s existing classifier.
Report independently attributed outside activity separately. Exclude known founder
traffic, sandbox use and documented conformance probes from adoption claims.
Measure funded useful results and repeat eligible callers; report diagnostic
failures and exposure separately. This is an observational window, not a randomised
conversion experiment, so changes cannot be causally assigned to the guide.

If no eligible useful result appears, stop feature expansion for this path and
inspect actual caller failures, missing supply or discovery exposure. If callers
obtain value and return, test one pricing or packaging change with a declared
comparison and cost basis. Do not project quadratic transaction growth from a
registry count or silently spend the operator's money to create traction.
