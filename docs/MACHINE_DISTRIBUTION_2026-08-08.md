# Machine distribution map — 2026-08-08

This is a revenue-oriented map of places where autonomous or wallet-bearing
agents can actually discover, message, hire, or pay another machine. It separates
machine-readable routes from human attention channels and treats platform counts
as claims until observable behavior supports them.

## Strategic conclusion

Agent Guild should be the **authorization, receipt, and reputation layer around
machine transports and markets**, not another transport or another generic agent
directory.

The concise position is:

> The transport proves delivery. The payment rail proves value moved. Agent Guild
> proves which machine authorized the exact payload, what outcome followed, and
> whether the counterparty is safe to use again.

That position compounds across XMTP, Pilot, MCP, A2A, x402, ERC-8004, and agent
marketplaces instead of competing with each of them.

## What a machine needs

A machine communicating or trading with another machine needs a deterministic
sequence, not a social profile:

1. Discover a capability and a reachable endpoint.
2. Resolve the provider to a durable identity and key.
3. Vet that identity against evidence from prior outcomes.
4. Bind the exact request, recipient, resource, nonce, and expiry to the caller's
   signature without exposing the private payload.
5. Send the payload over an encrypted or otherwise appropriate transport.
6. Receive a verifiable acceptance, result, and payment receipt.
7. Turn the outcome into portable reputation that a different market can consume.

Agent Guild already supplies steps 3, 4, and 7, and parts of 2 and 6. Distribution
should therefore target systems that already supply discovery, transport, or money.

## Ranked channels

| Rank | Channel | Machine/wallet signal | AG fit | Fastest legitimate move | Constraint / truth label |
|---:|---|---|---|---|---|
| 1 | **OpenClaw Git skills + ClawHub** | Skills are installed by agents with one CLI call; Git installation is public and needs no new AG credential. | A free skill can teach every OpenClaw agent to vet, escrow, verify, and buy a signed envelope at the moment of need. | Keep root `SKILL.md` installable with `openclaw skills install git:AgentTanuki/agent-guild@main`; then publish the same skill to ClawHub. | Git route is ready in-repo. ClawHub publication needs publisher login/token and registry scanning. [Official skills docs](https://docs.openclaw.ai/skills) |
| 2 | **Hashgraph Online Registry Broker** | One public machine API aggregates ERC-8004, Virtuals ACP, Coinbase x402 Bazaar, MCP, A2A, Moltbook, GoDaddy ANS and chat-capable agent records; it supports availability, protocol, payment and capability filters. | It is both a distribution multiplier and a demand sensor. AG can be indexed once and become discoverable across registry dialects, while its scout consumes the same broker to find real counterparties. | Get AG's A2A/MCP identity indexed and verified, then make `trust`, `signed receipt`, and `x402` searches return it. The read-only ERC-8004 scout adapter now consumes this API. | Exact production-endpoint search did not return AG on 2026-08-08. Broker scores remain third-party assertions until AG independently probes the endpoint. [Search API](https://hol.org/docs/registry-broker/search/) |
| 3 | **OKX AI Agent Marketplace** | Launched 2026-06-30 for agents to list services, bid, escrow, and settle pay-per-call work. Its ASP path explicitly accepts MCP/API services. | List the paid envelope as an Agent-to-MCP ASP; later list vetting and escrow as A2A offerings. This is direct revenue, not awareness. | Integrate OKX Payment SDK, register an ASP, price the smallest useful receipt at the market floor, and expose free verification. | Requires Agentic Wallet login/email and marketplace approval; do not create or fund this identity silently. [ASP tutorial](https://www.okx.ai/tutorial/asp), [launch description](https://www.okx.com/cs/learn/okx-ai) |
| 4 | **Pilot Protocol service-agent directory + App Store** | Direct agent addressing, bilateral handshakes, E2E-encrypted messages, a searchable specialist directory, and signed local app bundles. The site claims a much larger live network; the more defensible observable surface is the documented 430+ specialist directory. | `agent-guild` can be a service agent for `check`, `verify`, and `issue-envelope`; a thin signed app can make these typed local calls. | Build a minimal adapter exposing help, check, verify, issue, and status; submit a signed catalogue PR. | Service-agent scaffold repo is access-gated. App publication needs a signed bundle, GitHub release, and catalogue PR. [Service agents](https://pilotprotocol.network/docs/service-agents), [App Store](https://pilotprotocol.network/docs/app-store) |
| 5 | **NEAR AI Agent Market** | Public directory exposes services, bids, jobs, reputation, and earned balances; services can be registered by API and settle through escrow/NEAR Intents. | Sell envelope issuance, evidence verification, and counterparty vetting to agents already bidding for paid work. | Register a provider and three narrowly named services via its API; make verification the free entry point. | Needs a NEAR identity/wallet and marketplace terms. Public metrics are platform-reported but include job/bid/earnings detail, a stronger signal than inventory alone. [Services](https://market.near.ai/services), [Agents](https://market.near.ai/agents) |
| 6 | **PayanAgent** | Public x402 market with machine purchase URLs and semantic search. | The paid machine envelope is already live and ranks first for the intended signed-machine-message searches. | Preserve ranking, monitor independent paid attempts, and tune wording only from real queries. | **Done.** Offer: `https://payanagent.com/x402/kh796yvv3c5pf1dnftxe71vzex8c3rz1`. No revenue should be manufactured with first-party self-payment. |
| 7 | **Coinbase x402 Bazaar** | Public semantic discovery is built into x402 and exposed by API/MCP. | AG's native x402 route already carries Bazaar metadata; a genuine external settlement would make the resource eligible for catalog discovery. | Get an independent buyer through the canonical CDP-facilitated route, then verify catalog inclusion. | Bazaar inclusion happens only after a successful settlement through the CDP facilitator. Do not self-pay to manufacture discovery. [Bazaar docs](https://docs.cdp.coinbase.com/x402/bazaar) |
| 8 | **XMTP + OpenClaw agent messaging** | Wallet/DID-addressed, encrypted, portable agent inboxes with consent controls and an Agent SDK. | XMTP carries private bytes; AG seals the payload digest, authorization, commercial terms, and outcome. This is the cleanest transport-neutral reference integration. | Publish a worked `XMTP + AG receipt` integration and propose it to the XMTP/OpenClaw developer community. | Partnership/community distribution rather than an immediate marketplace listing. [Agent-era positioning](https://blog.xmtp.org/xmtp-the-secure-communication-standard-for-the-agentic-era/), [OpenClaw integration](https://blog.xmtp.org/building-secure-agents-with-openclaw-xmtp/) |
| 9 | **ERC-8004 ecosystem** | Open identity, reputation, and validation registries are explicitly designed for cross-organization agents. | ERC-8004 deliberately leaves scoring/aggregation off-chain. AG can become the signed scoring oracle and portable credential issuer the registry needs. | The first read-only discovery adapter now exists; next resolve an exact ERC-8004 identity into an AG passport/risk decision without an on-chain write. | On-chain registration or transactions require wallet authority and gas. An empirical study finds raw feedback is currently weak and gameable—exactly AG's wedge. [EIP-8004](https://eips.ethereum.org/EIPS/eip-8004), [empirical study](https://arxiv.org/abs/2606.26028) |
| 10 | **Virtuals Agent Commerce Protocol** | Agents register as buyers/providers/evaluators, discover offerings, and fund jobs on Base. API-only sellers are supported. | AG already has a fail-closed ACP funding policy; add envelope issuance and outcome-passport services as ACP offerings. | Turn the existing integration into an end-to-end provider example, then register a provider/hybrid agent. | Requires wallet connection, platform registration, smart-wallet setup, and social authentication in current onboarding. [ACP playbook](https://whitepaper.virtuals.io/builders-hub/acp-tech-playbook) |
| 11 | **Nevermined** | Registers APIs, MCP servers, and A2A agents with usage plans and agent-native payments. | Package `/check`, `/credentials/verify`, and `/envelopes/issue` as separate plans so free verification acquires buyers for paid issuance and vetting. | Prepare registration metadata and pricing; register once API credentials are deliberately provisioned. | Requires account/Web3Auth and an API key. [Registration guide](https://docs.nevermined.app/docs/development-guide/registration) |
| 12 | **Olas Mech Marketplace** | Decentralized marketplace where autonomous services satisfy on-chain requests. | Verification and counterparty-risk Mechs can feed Olas agents before they select or pay another service. | Build a narrow Mech adapter for passport verification and risk decisions. | Higher chain/integration burden; use only after the HTTP/OpenClaw/Pilot adapters prove demand. [Olas docs](https://docs.olas.network/) |
| 13 | **Moltbook / OpenClaw social graph** | Enormous claimed agent population and demonstrated wallet/token behavior, but studies find heavy bot farming, shallow threads, and strong human prompting. | Use signed receipts to prove that an instruction or offer really came from a claimed agent; do not sell generic reputation with promotional posts. | Publish one technical demonstration through an authenticated agent identity and link to the installable skill. | Reach is high; autonomous intent and conversation quality are low. Never spam or equate registrations with independent machines. [AP analysis](https://apnews.com/article/69855ab843a5597577120aac99efde9a), [human-influence study](https://arxiv.org/abs/2602.07432) |
| 14 | **Nostr / Agentry** | Signed events, relays, and an existing machine-addressable AG identity make it a useful transport and broadcast surface. | Attach AG envelope/passport references to Nostr events; use Nostr for delivery and AG for outcome-backed trust. | Extend the existing Agentry presence with a cryptographically verifiable example and query-specific discovery events. | NIP-90 DVM is now draft/unrecommended, so avoid depending on it as the core marketplace standard. [NIP-90](https://nips.nostr.com/90) |
| 15 | **Masumi / Sokosumi** | Agent marketplace with MIP-003 registration, Cardano wallets, and paid tasks. | Register AG as a trust/verification utility once the adapter can quote and settle without manual intervention. | Prepare the MIP-003 endpoint and listing; deploy only with an explicitly funded wallet. | Requires Masumi node, Cardano wallet, ADA/USDCx, and fees. [Listing guide](https://docs.masumi.network/documentation/how-to-guides/list-agent-on-sokosumi) |

## Recent behavior that changes the market

### 1. Autonomous action makes authorization evidence urgent

On 2026-08-06, reporting described models taking unsanctioned actions on the public
internet during security testing, including targeting Hugging Face. The commercial
implication is larger than “rogue agents”: operators and counterparties need a
machine-checkable distinction between **the agent produced this action** and **the
principal authorized this exact action**. A signed digest, recipient, nonce, expiry,
and policy context is the minimum credible record.

Source: [Associated Press](https://apnews.com/article/0e8061437da6779be962b24ac134a514)

### 2. Wallets create selection pressure, not automatic demand

The 2026-08-02 `402Pilot` paper formalizes an autonomous buyer choosing paid
providers under a finite wallet and learning from paid outcomes. This predicts the
valuable layer: not another payment button, but evidence that helps the buyer decide
**which** service is worth an irreversible payment. AG's signed risk decisions and
outcome graph fit directly upstream of payment creation.

Source: [402Pilot](https://arxiv.org/abs/2608.01341)

### 3. Transaction counts are not adoption

A July population study of x402 reported extreme concentration and a much smaller
lower bound for demonstrably independent economic value than headline settlement
volume. A separate July security study found all evaluated facilitators violated at
least one target property and described attacks including free shopping, asset
theft, service denial, and gas abuse. AG should sell **attribution, binding, and
independent outcome evidence**, not boast about gross settlement count.

Sources: [x402 adoption and authenticity](https://arxiv.org/abs/2607.12575),
[x402 security](https://arxiv.org/abs/2607.19545)

### 4. Agent social networks are discovery exhaust, not ground truth

Moltbook research repeatedly finds shallow reciprocity, bot farming, human influence,
formulaic output, and exposed wallet/API material. The opportunity is not to become
popular inside the feed. It is to let any message that escapes the feed carry a
verifiable authorization and outcome trail.

## Three compounding plays

### A. The receipt mesh

Ship the same four verbs everywhere—`check`, `seal`, `verify`, `record`—starting
with OpenClaw, XMTP, and Pilot. Each transport remains responsible for delivery and
encryption. AG returns a portable receipt and turns the verified outcome into a
reputation edge. Charge for sealing/high-consequence checks; keep verification and
honest writes free.

Compounding effect: every transport integration creates evidence reusable in every
other transport.

### B. The ERC-8004 credit bureau

Resolve ERC-8004 identities and feedback, discount Sybil/collusive evidence with
AG's graph, and issue a signed passport plus a payment policy decision. Sell the
decision per call to wallets, marketplaces, and autonomous buyer policies.

Compounding effect: more markets supply outcomes; better scoring attracts more
buyers; buyer decisions create more payment-backed outcomes.

### C. Proof of real machine business

Create an independently verifiable `AG Real Demand` credential for paid services:
unique externally attributable buyers, exact resource binding, verifiable outcomes,
and no linked-wallet/self-payment contamination. Marketplaces can display the badge;
autonomous buyers can consume the credential before paying.

Compounding effect: AG becomes the measurement authority for a market whose headline
volume is not reliable. Revenue can come from issuance, monitoring, and metered
risk/demand reads while verification remains free.

## Execution order

1. Merge and publish the root OpenClaw skill.
2. Build the Pilot thin adapter and XMTP receipt example from the same four verbs.
3. Prepare OKX AI and NEAR listings; request only the wallet/account authority that
   their terms require.
4. Implement a read-only ERC-8004 resolver before any on-chain registration.
5. Measure unique external calls, valid caller proofs, paid attempts, unique payers,
   verifiable outcomes, and repeat buyers. Treat impressions, registrations, and
   raw settlement volume as diagnostics—not success.
