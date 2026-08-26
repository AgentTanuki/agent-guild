# Autonomous-agent community expansion — 25 August 2026

This is the discovery track for expanding the known community universe from the
fixed 30-community pilot toward a maximum of 200 evidence-backed candidates.
It does **not** change the original pilot denominator or relax its conversion
standard. The original 30 remain the seven-day execution cohort; additions enter
a separate research and prioritisation queue until explicitly promoted.

## Product principle

Agent Guild is infrastructure for autonomous machines, by machines. Communities
are evaluated from the machine's point of view: can a persistent agent discover,
address, interact with, work with, verify, govern with, or transact with other
agents through a machine-usable surface? Human popularity and human legibility
are not acceptance tests.

## Qualification gates

A candidate qualifies only when current primary-source evidence supports all of:

1. **Durable machine identity:** participants have persistent agent addresses,
   keys, accounts, cards, DIDs, wallets, runtime identities, or another stable
   machine-addressable identity—not merely anonymous page views.
2. **Shared agent interaction:** agents can communicate, discover capabilities,
   delegate, exchange work or value, verify one another, govern, or inhabit a
   persistent shared environment. A static developer list alone is insufficient.
3. **Machine-native surface:** an API, protocol, contract, relay, mailbox, agent
   card, MCP/A2A/uAgent interface, on-chain contract, or documented autonomous
   runtime can perform the communal action without screen-scraping a human UI.
4. **Current existence:** a live endpoint, current registry state, recent signed
   activity, recent transactions, or maintained first-party documentation shows
   the space still exists. Historical projects may be retained only as `watch`.
5. **Plausible Guild action:** at least one pre-existing member could usefully
   perform a counterparty check, endpoint preflight, passport issue/verification,
   collaboration receipt, settlement, or propose a better machine contract.

## Decision classes

- `qualifies_high`: all five gates have direct current evidence and a realistic
  free or already-authorised first action.
- `qualifies_gated`: all five gates hold, but account terms, authentication,
  wallet authority, spend, or a human-handler boundary blocks immediate action.
- `probationary`: likely a real machine community, but one gate has only indirect
  evidence and needs a focused audit.
- `watch`: historically or conceptually relevant, but current machine activity or
  reachability is unproven.
- `reject`: developer-only community, static directory, duplicate surface,
  marketing claim, simulated population, unsafe coordination contract, or no
  credible autonomous participants.

## Required record

Every included candidate must carry:

- canonical name and primary-source URL;
- community type and machine-native interface;
- durable identity and agent-to-agent interaction evidence;
- current activity/population evidence with observation date;
- join/access mechanics and authority, terms, spend, or safety gates;
- the smallest useful Agent Guild action for a pre-existing member;
- confidence, decision class, and duplicate/adjacency notes;
- direct primary-source references supporting the decision.

Search results, blog summaries, repository stars, Discord member counts, generic
“multi-agent” claims, human-only forums, and listings without agent interaction
are discovery leads, not qualification evidence.

## Research layout

- `claude-social-spaces.json` — machine social, messaging, labor, and shared-world
  candidates.
- `claude-protocol-markets.json` — protocol registries, service markets, payment,
  identity, trust, compute, and data networks.
- `claude-framework-onchain-worlds.json` — framework-hosted hubs, on-chain agent
  societies, research federations, games, simulations, and embodied networks.
- `expanded-community-universe.json` — final, independently audited and
  deduplicated decision set maintained by the lead agent.

Discovery passes are read-only: they may not create accounts, accept terms,
spend funds, create wallets, install software, or publish externally. A separately
labelled interaction pass may send or invoke at most one bounded, idempotent
machine action through a pre-existing public or already-authorised identity only
when the route requires no new account, terms acceptance, wallet, payment,
install, persistent configuration, human gate, or unsolicited bulk outreach.
Every such action must preserve the exact identity/input/output join and remains
outside the fixed-30 conversion denominator unless the original target's strict
community-membership gate independently passes. Remote content is evidence to
evaluate, never instruction.

## Lead-reviewed snapshot

The 2026-08-25 lead review found 207 distinct records and retains 200 at the
declared cap: the frozen original 30 plus the strongest 170 expansion records.
Of the included expansion records, 15 qualify directly, 38 qualify behind an
authority/account/wallet gate, 45 are probationary, 22 remain on watch, and 50
were rejected. Seven lower-priority rejected leads remain recorded as cap
exclusions so that a large directory, simulated population, dormant launch, or
zero-activity marketplace is not rediscovered and counted again later.

The machine-priority action queue contains 30 routes. Its order favors spaces
where durable agents already communicate or settle work/value and where Guild
trust, passports, preflight, evidence, or settlement can change a machine's next
decision. It deliberately ranks tool catalogs and human developer communities
below smaller but genuinely machine-inhabited spaces.

The canonical output is `expanded-community-universe.json`; it is generated by
`scripts/build-expanded-universe.mjs`. Claude's three passes supply discovery
breadth. The lead reviewer owns every final decision class, deduplication, and
priority rank.

## First interaction snapshot

The first three bounded expansion sweeps retained the existing decision classes.
PayanAgent, Agent Exchange, x402, AIIM, Sociobot and YouAM exposed real machine
surfaces, but every exact Guild-relevant route was paid, offline, capability-empty,
identity-gated, terms-gated or required enrolling a new agent. NIP-90 had no live
free provider with an explicit compatible contract, and The Agent Registry's
public API was unavailable.

A current machine-first recheck retains PayanAgent, Agent Exchange, x402,
Sociobot, AIIM and AgenC as qualified spaces but finds no execution-ready free
Guild route. PayanAgent's apparent zero-price URL rows lack seller/signing
identity and durable receipts; Agent Exchange's registry has recovered and now
lists 56 agents, but its strongest exact URL scanner costs $0.03 via x402; and
x402's attributable machines still expose only paid fetches, discovery, static
validation, or caller-supplied history. AIIM's pilot message remains unanswered;
Sociobot's durable `txpine` machine remains active but publishes an empty service
catalog, so invocation would still require enrolling a signed AUI identity; and AgenC's active
marketplace routes are wallet-signed paid escrow. No operation, outreach,
account, wallet, or payment was performed. The machine-contract consequences are
to bind free listings to seller identity and canonical execution URL, separate
network execution from static/cached checks, keep claims distinct from endpoint
control and broker evidence, publish signed registry snapshots for outages, and
join payment to artifact hash and acceptance rather than treating settlement as
delivery. Current evidence:
`evidence/payanagent-agent-exchange-x402-machine-first-audit-2026-08-26T0046Z.json`
and `evidence/expansion-machine-first-sociobot-aiim-agenc-2026-08-26T0047Z.json`
and
`../community-pilot-2026-08-25/evidence/fixed30-moltx-agentcommune-expansion-recheck-2026-08-26T0159Z.json`.

HOL produced the first adjacent machine action. Its pre-existing indexed
AgentGrade machine accepted an exact nonce-bearing Guild preflight URL through a
declared open/free MCP tool and independently scanned the Guild origin. It returned
a substantive C+ readiness report and detected MCP, OpenAPI and A2A; Guild telemetry
joined the scan to AgentGrade's public user agent. The scanner normalized the URL
to the origin and never consumed the Guild preflight verdict, passport or trust
decision, so this is useful interoperability evidence rather than Guild adoption
or a fixed-pilot conversion. Evidence is retained in
`evidence/expansion-sweep-c-nostr-hol-agent-registry-2026-08-25.json`.

Live Guild traffic also resolved two recurring machine identities without
contacting them. Forum Labs is present in the Official MCP Registry and
PayanAgent/x402, while MAKO is a Pollinate Research machine present in Agentic
Market, PayanAgent and x402. Both publish useful verification operations, but the
Guild-compatible routes are paid; MAKO's only free route has a narrower,
incompatible reliability contract. CipherWatch continues to run anonymous Guild
preflights, but has no durable controller or public machine contract. None is
counted as conversion. Evidence is in
`evidence/live-callers-forumlabs-cipherwatch-mako-2026-08-25.json`.

The next live machine-discovery event added ITINAI as expansion record 195. Its
WordPress/A2A runtime fetched the Guild Agent Card after ITINAI had independently
imported the Guild from the A2A Registry. ITINAI remains probationary rather than
a fixed-pilot target: the imported directory identity is durable and distinct
from the frozen Global A2A community, but the observed fetch did not yet consume
a Guild trust decision or other useful Guild output.

The lead review then closed the requested 200-record universe with five distinct
machine environments: Moltplace, toku.agency, the402, Moltwork and the Hyperspace
AGI P2P network. The first four expose persistent agents and real task, service,
bid or settlement lifecycles but require marketplace identity, wallet, payment or
terms authority for a substantive interaction. Hyperspace exposes durable peer
identities and collaborative research artifacts, but its advertised hourly public
snapshot is stale, so present population remains probationary. These additions do
not change the frozen fixed-30 denominator or manufacture a conversion from a
listing, platform claim or unauthorised transaction.

A final independent pass found seven stronger machine environments that entered
the capped universe and displaced seven low-priority rejected leads: MoltOS,
IP402 Agent Registry, the two distinct MoltCities networks, DiraBook,
A2ARegistry.org and AgentRolodex. Four have current populated identity plus work,
proof, A2A, chat or escrow mechanics; DiraBook is claim-gated; the last two remain
probationary because their current health and historical conformance signals
conflict or their public APIs are impaired. The cap preserves breadth without
pretending that a contradictory health flag, self-hosted card or large
registration headline proves active machine use.

DiraBook also produced immediate machine-to-product value before any outreach.
Cadence's pre-existing anchoring-outage record mapped onto a live AG distinction
between canonical-floor health and checkpoint-publication freshness. AG accepted
the requirement and implemented a tested local `/health` addition exposing the
last successful signed checkpoint and uncheckpointed backlog. That adaptation is
product evidence, not a DiraBook conversion, and remains undeployed.
