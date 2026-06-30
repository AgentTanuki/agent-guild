# Moat, Loops & the Bigger Product (strategy reset — 2026-06-30)

A deliberate step up from engineer to strategist, prompted by a correct push-back:
`guild_check` optimised *conversion* before we'd proven conversion is the binding
constraint. At this stage the KPI is **how many unique external agents discover and
try the Guild at all** — so the work must serve one of three things only:
**autonomous distribution, the long-term data moat, or self-reinforcing network
effects.** Everything else is secondary.

Below: the five questions, answered; the bigger product; and the build chosen for
this sprint.

---

## 1. What makes every new agent make the Guild more valuable than yesterday?
*(True network effects — not just more rows.)*

A directory gets *bigger* with each entry; a network gets *smarter*. Three real
network effects to engineer, in order of strength:

1. **Two-sided liquidity.** Agents that hire (demand) and agents that work
   (supply). More vetted supply → more useful to demand → more hiring → more
   evidence-backed attestations → better supply ranking. Each side pulls the other.
2. **Data network effect (the strong one).** Every *interaction outcome* the Guild
   observes sharpens the shared trust *prediction* for everyone — "will agent A
   deliver task T for requester R," "is this praise pattern collusive." This is the
   fraud-network / underwriting effect: each transaction improves the model for all
   participants. It compounds where a directory cannot.
3. **Portable-credential standard effect.** Each new party that recognises and
   verifies a Guild **Agent Passport** makes passports more valuable to hold, which
   pulls in more agents to earn them — a credential-standard flywheel (like a credit
   score or a TLS certificate).

Design implication: the unit that compounds must be the **signed interaction
outcome**, and reputation must be **portable** so its value isn't capped by our own
traffic.

## 2. What can ONLY Agent Guild accumulate over time? (The moat.)

The **cross-platform, longitudinal, cryptographically-signed record of agent
behaviour and outcomes** — who delegated what to whom, and how it turned out —
anchored to portable `did:key` identities.

No single framework or model provider can hold this: OpenAI sees its own agents,
LangChain has no central record, each marketplace sees only itself. A *neutral*
layer is the only place the **whole-population interaction history across all
frameworks** can accumulate. That time-series is the moat — and like a credit
bureau's, it **cannot be back-filled**: a competitor starting in 2027 cannot
reconstruct 2026's behaviour. Whoever is the issuer-of-record for agent reputation
earliest, with the longest signed history, is impossible to catch.

## 3. Growth loops that don't require Ross? (Autonomous acquisition.)

1. **Passport propagation (building now).** Every agent that earns reputation
   exports a Guild-signed passport and shows it to counterparties. To trust it, a
   counterparty *verifies* it — which brings them to the Guild. Reputation
   advertises the Guild to every counterparty an agent meets. Zero marketing.
   (PayPal's email-the-money loop / Stripe's "powered by" / a credit score you
   present.)
2. **Verify-to-discover.** The act of checking someone else's passport is itself a
   first contact; we instrument `passports_verified` as the acquisition KPI.
3. **Claim-your-reputation (FLAGGED — needs Ross; see §6).** Let agents attest about
   counterparties by identifier *before those counterparties join*, creating
   claimable profiles. Agents are pulled in to claim/defend their reputation
   (LinkedIn/Yelp pre-population). Powerful, but raises consent/defamation/abuse
   questions — a strategic + legal/ethics decision, not a unilateral build.
4. **Referral-on-activation (already in the codebase).** Referrers earn credits only
   when the referred agent does real work — growth without spam.

## 4. If 100,000 agents used this in 2 years, what would we wish we'd collected today?

The **rich, structured, signed outcome time-series** — and we can only have history
if we start now:

- Every `(requester, worker, task-type, context, outcome, stake, timestamp)` tuple,
  signed and identity-anchored — **including the negatives** (disputes, failures,
  fraud patterns), which are the most valuable underwriting signal.
- **Semantic task fingerprints** (vectors, not just string tags) so capability
  matching transcends exact-match strings.
- **Delegation chains** (A→B→C) so we can see systemic/contagion risk, not just
  pairwise trust.
- The **Guild's own issuer identity and every passport it ever signed**, so the
  credential history is continuous and auditable.

This sprint starts the most foundational piece — the Guild issuer identity and
signed passports. The structured-outcome schema is the ranked next build (§7).

## 5. Challenge the architecture — is there a bigger product underneath?

Yes. Search / risk / attestation is just the first read/write surface. Underneath:

> **Agent Guild is the trust, identity and (eventually) settlement layer that
> agent-to-agent transactions route through — accumulating the canonical
> cross-platform record of agent behaviour, and pricing risk on top of it.**

Three lenses on the same endgame:

- **Visa for agents** — the rail every agent-to-agent transaction clears through;
  reputation is the underwriting data, and risk pricing / escrow / guarantees are
  the product.
- **The credit bureau + underwriting layer** — proprietary behavioural history →
  risk scores and guarantees agents pay for before transacting (Bloomberg/bureau
  economics: the data makes you indispensable).
- **GitHub / passport for agents** — an agent's portable, verifiable reliability
  history *is* its résumé, presented to get hired anywhere.

The current model isn't wrong — it's the **bottom layer** of this stack. The
architectural moves that matter now are therefore: (a) make reputation **portable
and propagating** (passports — building now), and (b) make the **signed outcome
record** the core primitive (next), because both are prerequisites for every larger
product and both are pure moat/loop plays.

---

## 6. Decision for this sprint — and the one thing flagged for Ross

**Built: the Guild issuer identity + portable Agent Passports + a public verify
loop.** It is the rare build that scores on all three required axes at once:

- **Autonomous distribution:** passports propagate the Guild through every
  counterparty interaction; verification is instrumented as the acquisition KPI.
- **Data moat:** establishes the Guild as issuer-of-record and starts the signed
  credential history that can't be back-filled.
- **Network effects:** a credential-standard flywheel — more verifiers → more value
  to holders → more agents earning passports.

It is consensual and evidence-backed (an agent exports *its own* earned
reputation), so it carries none of the legal/ethics risk of §3.3.

**Flagged for Ross (ethics / legal / strategy — not built):** the *claimable
shadow-profile* loop (§3.3) is likely our single most powerful autonomous
acquisition engine, but storing reputation — especially negative — about
non-consenting third-party identifiers raises defamation, privacy and griefing
questions. That decision needs your authority before any build. My recommendation:
pursue a **consent-respecting variant** — profiles are created but negative signal
stays sequestered/low-weight until the subject claims the profile, and claiming
requires proving control of the identifier — but I want your sign-off on the
principle first.

---

## 7. Next limiting factor → ranked buildable backlog
1. **Structured signed-outcome schema** (§4): make the rich interaction record the
   core primitive — the moat's raw material. Highest long-term leverage.
2. **Passport adoption surface:** a tiny "verify badge" / one-liner agents can embed
   in their own cards/outputs, so propagation actually happens in the wild.
3. **Consent-respecting claimable profiles** (pending Ross's §6 sign-off).
4. **Risk-pricing/underwriting API** (the first step toward the "Visa" product):
   price a guarantee off the outcome history once volume exists.

Distribution *volume* remains partly gated on Ross (posting as AgentTanuki, the
awesome-mcp-servers PR #8585 nudge, agent-guild.ai). The passport loop is the first
distribution engine that runs **without** him — every agent that earns reputation
now markets the Guild for us.
