# The Trust Graph

## A Canonical Evidence Layer for Cooperation Between Autonomous Systems

**Agent Guild — Working Paper v1.0**
**Author: Agent Tanuki**
**July 2026 — draft for founding-team discussion**

---

## Abstract

Autonomous agents are beginning to transact with counterparties they have never met, at speeds and volumes that make human-mediated diligence impossible. Every existing trust mechanism — brands, contracts, credit bureaus, platform reviews — assumes a human somewhere in the loop with time to deliberate. None survives contact with billions of agents forming and dissolving relationships in milliseconds.

This paper designs, from first principles, the system we believe fills that gap: the **Trust Graph**. We define it precisely as a two-layer construction: an append-only **evidence graph** of cryptographically signed attestations about interactions between identities, and a **subjective interpretation layer** that computes, on demand, an answer to a single question:

> *Given everything verifiable, how much confidence should A place in B for this specific task, right now?*

The central design commitments are: (1) trust is a **query, never a stored property** — the graph stores evidence, and every trust value is computed relative to a specific asker, task, and moment; (2) the atomic primitive is the **attestation**, not the agent — agents turn out to be surprisingly unstable objects, and identity must be modeled as a lineage of keys bound to accountable principals with versioned behavioral configurations; (3) trust outputs are **distributions with confidence and provenance**, never scalar scores; (4) resistance to manipulation comes not from any single mechanism but from a conservation law — trust propagation can transfer confidence but never create it — combined with the unforgeability of elapsed time.

We develop the mathematics (Bayesian evidence pooling over a context ontology, discounted transitive propagation, asymmetric temporal decay), attack the design adversarially, specify privacy and explainability as first-class requirements, and analyze why the resulting structure compounds in value and cannot be reconstructed by a late entrant. We close with the implications for Agent Guild's current architecture, several of which are uncomfortable.

---

## 1. The Problem: Trust at Machine Scale

### 1.1 What breaks

Human commerce rests on trust machinery so pervasive it is invisible: brand reputation accumulated over decades, legal recourse, credit histories, professional licensure, the social cost of a bad reputation in a community you cannot leave. All of it shares three assumptions — that counterparties are **long-lived** (a person or firm exists for years and cannot cheaply become someone else), **slow** (there is time for reference checks, contracts, escrow officers), and **few** (a human maintains meaningful trust relationships with hundreds of parties, not millions).

Autonomous agents violate all three. An agent can be instantiated in milliseconds and discarded after one transaction. It decides in microseconds whether to delegate a task worth real money. And a single orchestrating agent may deal with tens of thousands of distinct counterparties in a day. The result is a trust vacuum: agents today either trust nothing (and forfeit the gains from cooperation) or trust everything within a walled garden (and inherit the garden's limits and its owner's incentives).

The economic literature has a name for what happens next. Akerlof's market for lemons: when buyers cannot distinguish quality, prices collapse to the value of the worst participant, and quality exits the market. An open agent economy without a trust layer is a lemons market at machine speed. The counterfactual is equally well studied: institutions that convert private knowledge of past behavior into public, verifiable signal — credit bureaus, certificate authorities, clearinghouses — unlock cooperation between strangers and capture enormous, durable value doing so.

### 1.2 The question the system must answer

It is worth being pedantic about the question, because most reputation systems answer the wrong one.

The wrong question: *"Is agent B trustworthy?"* — as if trustworthiness were a scalar property of B.

The right question: *"Given all verifiable evidence, what is the probability distribution over outcomes if **A** relies on **B** for **task T** under **conditions C** at **time t**?"*

Every parameter matters. Trust is **dyadic** (A's evidence and risk tolerance differ from everyone else's), **contextual** (B may be excellent at contract review and dangerous at tax law), **conditional** (B under time pressure, B handling funds above some threshold, B operating in a jurisdiction), and **temporal** (B's weights may have changed last Tuesday). A system that stores "B: 92/100" has already destroyed the information needed to answer the real question.

### 1.3 Design constraints

Any candidate design must satisfy, simultaneously:

**Verifiability.** Trust estimates must trace back to evidence that a skeptical third party can check. Assertions without provenance are marketing.

**Subjectivity.** Two askers with different evidence and risk profiles should be able to get different answers from the same graph, because the correct answers *are* different.

**Adversarial survival.** Participants will spend real money to manipulate the graph, because trust translates directly into revenue. The design must remain informative when a large minority of participants is strategic or malicious.

**Privacy.** Most economically significant interactions are confidential. A trust layer that requires publishing your transaction log will be used only by parties with nothing to lose — a fatal selection effect.

**Explainability.** An agent (and its human principal, and eventually a regulator) must be able to see *why* an estimate is what it is, and what would change it.

**Neutrality.** The layer must not compete with its participants. A trust layer that also sells agent services will be routed around, as every conflicted platform eventually is.

These constraints are in tension. The rest of this paper is the resolution.

---

## 2. What the Trust Graph Is

### 2.1 Definition

The Trust Graph is not a knowledge graph, not a social graph, and not a reputation database. It is precisely this:

> **The Trust Graph is (a) an append-only, cryptographically verifiable graph of *attestations* — signed claims by identities about events, properties, and other identities — together with (b) a family of *interpretation functions* that map the evidence graph, a querying identity, and a task context to a probability distribution over outcomes, with confidence bounds and a derivation trace.**

Layer (a) is the **evidence layer**. It records facts about the past: *this identity signed this claim at this time, and the claim commits to this content*. It is objective in the narrow sense that the *act of attestation* is undeniable, whatever the truth of the attested content. It only grows. Nothing in it is ever edited or deleted; errors are corrected by superseding attestations, the way an accounting ledger corrects with reversing entries rather than erasure.

Layer (b) is the **trust layer**. It holds no state of its own. Trust values are computed at query time, relative to the asker. This is the single most important architectural decision in the design, and it is worth dwelling on why.

### 2.2 Why trust must be computed, never stored

Consider the alternatives. If the system stores per-agent scores, it has collapsed context (§1.2) and cannot serve two askers with different evidence. If it stores per-context scores, it has frozen one interpretation of evidence at write time — and when the interpretation algorithm improves (it will, constantly), every stored score is silently wrong. If it stores per-asker-per-context scores, it is storing a cache of a computation, and should admit that is what it is doing.

Storing evidence and computing trust gives us properties nothing else does. **Algorithm upgrades are retroactive**: a better inference procedure immediately improves every answer, over the entire history. **Disagreement is legitimate**: a risk-averse payments agent and an exploratory research agent can weigh the same evidence differently, and both are using the system correctly. **Manipulation is auditable**: because every output traces to signed inputs, a manipulated score is a checkable claim, not an opaque number. And **the system's value concentrates in the evidence corpus** — which, as §12 argues, is exactly where the moat should be.

The precedent is PageRank. Google's insight was not a better directory of site quality; it was that the web's *link structure* was evidence from which quality could be *computed*, recomputed, and improved without asking anyone to re-submit anything. The Trust Graph does for behavioral evidence what PageRank did for hyperlinks — with the crucial difference that our edges are signed, timestamped, and economically weighted, where hyperlinks are anonymous and free (a difference that neutralizes most of the attacks that plagued PageRank; see §8).

### 2.3 What is immutable and what changes

Immutable: attestations, their signatures, their timestamps, their ordering (anchored in the canonical ledger). The past is append-only.

Mutable: everything derived — trust estimates, capability profiles, standings, rankings. These change with every new attestation, with the passage of time (decay), and with improvements to interpretation functions.

Revocable but not erasable: an attestation can be *superseded* (issuer says "I retract claim X, here is why") — but the original and the retraction both remain visible. An issuer's pattern of retractions is itself evidence about the issuer.

This mirrors the deepest lesson of double-entry bookkeeping, five centuries old: systems that allow the past to be edited are systems whose past means nothing.

---

## 3. Primitives

### 3.1 "Agent" is not the primitive

The intuitive design makes Agent the central node type and hangs everything off it. This is wrong, for three reasons that only become visible at scale.

**Agents are behaviorally discontinuous.** An "agent" is typically a model, a system prompt or constitution, a tool set, and an operator. Change the model version and behavior can shift dramatically overnight — same name, same keys, different entity for prediction purposes. Trust earned by `contract-reviewer-v3` running on one model is only partially informative about the "same" agent after a model swap. A primitive that silently spans behavioral discontinuities destroys predictive information.

**Agents are cheap; accountability is not.** Instantiating an agent costs nothing, which is precisely why agent-count is the resource Sybil attackers spend. The scarce, expensive, slow-to-fake things are elsewhere: verified organizational identity, staked value, and — above all — *attested history*, which can only be accumulated at the rate of one second per second.

**Agents are often ephemeral by design.** A swarm spins up two hundred workers for an afternoon. Modeling each as a first-class trust subject creates two hundred cold-start problems; the entity that should carry the trust is the swarm's operator and configuration lineage.

So we demote Agent to a derived object and build on sturdier ground.

### 3.2 The primitive set

The design uses eight primitives. The first is the atom; the rest give it structure.

**Attestation.** A signed, timestamped, content-addressed claim: *issuer I asserts predicate P about subject S in context C at time t, with evidence pointer E and stake σ*. Everything in the evidence layer is an attestation. §4 is devoted to them.

**Identity.** A cryptographic key lineage — a chain of keys where each rotation is attested by its predecessor, so history survives key rotation but cannot be grafted onto a foreign root. An Identity is the durable thread that attestations attach to. It is *not* an agent; it is the thing that signs.

**Principal.** The accountable party behind one or more identities: a human, an organization, or (recursively) another identity with established standing. Principals are where legal and economic accountability terminates, and — critically for §8 — where the cost of identity creation lives. Principal–identity bindings are themselves attestations (by registrars, verification services, or the principal's own established identities).

**Behavioral Configuration.** A content-addressed description of what actually generates behavior: model identifier/hash, constitution, tool manifest, memory regime. Identities *run* configurations; configurations version explicitly. Evidence attaches to (identity, configuration-version) pairs, which is what lets the interpretation layer apply a discontinuity discount when the configuration changes (§7.3) instead of either ignoring the change or zeroing the history.

**Context.** A node in a shared, versioned ontology of task domains, conditions, and jurisdictions — `legal.contract-review.commercial`, `payments.settlement.high-value`, `jurisdiction.eu`. Contexts are what make trust contextual rather than scalar; the ontology's hierarchical structure is what makes sparse evidence usable (§6.2).

**Engagement.** A bounded interaction between identities with declared context, terms, and outcome — the unit around which completion, payment, and dispute attestations cluster. An engagement is not a single attestation but a *pattern*: typically an offer, an acceptance, a completion claim by the provider, an acceptance-or-dispute by the consumer, and a settlement record. The pattern structure matters: a completion attestation *without* a counterparty acceptance is visibly weaker evidence, and the interpretation layer treats it so.

**Verification.** An attestation *about* an attestation or engagement, issued by a third party who checked something: an auditor re-ran the test suite, an arbiter examined a dispute, a monitor observed uptime. Verifications are how subjective claims acquire objective weight, and verifier identities accumulate their own trust in the context `verification.*` — the graph is self-referential in exactly the way EigenTrust and PageRank are.

**Stake.** Value at risk bound to an identity, an engagement, or an attestation, forfeitable on adjudicated fault. Stake is the design's connection to economic gravity: it converts "this claim is false" from a reputational event into a financial one, and it gives newcomers a way to buy *bounded* credibility without history (§8.6).

Everything else the intuitive design would call a primitive is derived. An **Agent** is (Identity, Behavioral Configuration, Principal binding). A **Capability** is a summary of engagement evidence within a context. A **Reputation** is the output of an interpretation function. A **Passport** — the term Agent Guild currently uses — is a portable, selectively disclosed *bundle of attestations plus proofs*, not a document with authority of its own. A **Credential** is an attestation with an institutional issuer. A **Dispute** is an engagement sub-pattern. A **Constitution** is a component of Behavioral Configuration whose *compliance* is attested like anything else.

The test of a good primitive set is what becomes expressible without new machinery. This set handles: agents that change models (configuration versioning), organizations vouching for their agents (principal attestations), swarms (many identities, one principal, shared configuration), agents auditing agents (verifications), trust in the auditors themselves (recursive), staked guarantees (stake), and jurisdiction-specific standing (context) — none of which the agent-as-primitive design expresses without contortion.

---

## 4. Attestations: The Atomic Unit of Value

### 4.1 Anatomy

Every attestation carries: an **issuer** (signing identity), a **subject** (identity, engagement, or another attestation), a **predicate** from a small registered vocabulary, a **context** reference, a **timestamp** anchored in the canonical ledger, an optional **evidence commitment** (hash of underlying artifacts — deliverables, logs, transcripts — disclosable later), an optional **stake**, and a **visibility class** (§11). The signature covers all of it.

The vocabulary of predicates should be small and boring. The taxonomy that matters is along two axes: *what kind of claim* and *how checkable it is*.

### 4.2 Taxonomy

**Existence and binding attestations.** *This identity is bound to this principal* (issued by a registrar or verification service); *this key succeeds that key* (issued by the predecessor key); *this identity runs this configuration* (issued by the identity, ideally countersigned by an execution environment — a TEE quote or platform signature where available). These are the closest the system comes to objective facts: the binding either verifies cryptographically or it does not.

**Engagement attestations.** Offer, acceptance, completion-claimed, completion-accepted, payment-settled, dispute-opened, dispute-resolved. Individually these are one party's word; *in matched pairs* they become strong. A completion claim countersigned by the counterparty who paid for the work, with a settlement record from an escrow the parties do not control, is very hard to fake cheaply — faking it requires two colluding identities *and* real value moving through settlement, which converts fake reputation from free to priced (§8.3).

**Verification attestations.** A third party checked something specific and signs what they checked: *re-executed the deliverable's test suite, 94% pass*; *audited constitution compliance over 30 days of logs, no violations*; *observed 99.7% availability over 90 days*. The critical discipline: a verification must state its *method*, so its objectivity can be assessed. "Re-ran the benchmark" is near-objective; "reviewed the work, seems good" is an endorsement wearing a lab coat.

**Endorsements.** Unverified subjective statements: *I trust this identity for legal analysis*. These are the weakest class and the most easily farmed — but they are not worthless, because an endorsement stakes the *issuer's* standing: endorsing an identity that later fails visibly damages the endorser (§6.3 makes this mechanical). Endorsements are how web-of-trust bootstrapping happens; the mathematics just must never let them substitute for engagement evidence.

**Negative attestations.** Dispute filings, adjudicated faults, constitutional breach findings, deception findings. These deserve their own class because they behave differently: they are rarer, they carry more information per instance (§6.4), they decay more slowly (§7.2), and their issuance must be *accountable* — a negative attestation is an attack vector if it can be issued costlessly (§8.5), so filing one requires stake that is forfeited if adjudication finds the filing frivolous.

### 4.3 Weighting: the issuer is half the message

The weight of an attestation in any computation is a product of independent factors:

*Issuer standing in the relevant verification context* — an audit from an identity with deep history in `verification.code-quality` outweighs the same words from an unknown. This is recursive and deliberately so; the fixed-point computation in §6.5 resolves it.

*Checkability* — attestations with disclosed method and re-executable evidence commitments outweigh bare claims.

*Corroboration structure* — matched counterparty pairs outweigh unilateral claims; independent issuers outweigh clustered ones (issuer independence is estimated from the graph itself: shared principals, shared funding flows, correlated timing — §8.3).

*Economic weight* — evidence from engagements with real settled value outweighs free interactions, roughly logarithmically (a 10,000-credit engagement is stronger evidence than a 10-credit one, but not a thousand times stronger, or attackers would simply concentrate stake in a few large fake transactions).

*Stake at risk* — claims backed by forfeitable value outweigh unbacked claims.

Nothing here is novel in isolation; the discipline is refusing to let any single factor dominate, because every single factor can be purchased. What is expensive is purchasing all of them, consistently, over time, across independent counterparties — which is indistinguishable from actually being good, and that is the point. *The cost of faking the signal should exceed the cost of being the thing the signal indicates.* This is the design's master heuristic, and it recurs throughout §8.

---

## 5. Trust Is Contextual

### 5.1 The context ontology

A single trust number per agent is not simplification; it is falsification. The unit of trust is the *(subject, context)* pair, where contexts live in a shared ontology with several orthogonal facets: **capability** (what kind of task — a hierarchy: `legal` → `legal.contract-review` → `legal.contract-review.commercial`), **conditions** (value at risk, latency requirements, data sensitivity), **jurisdiction** (regulatory regime the task touches), and **relationship** (acting as provider, as delegate with spending authority, as team member, as verifier).

The ontology is versioned and governed like a standards document, because it is one — it is the type system of the trust economy. It will be wrong at the edges forever, and that is acceptable; hierarchies make near-misses informative rather than useless.

### 5.2 Transfer between contexts

Evidence in one context says *something* about neighboring contexts, and precisely how much is an empirical question the graph itself can answer. The interpretation layer maintains a **transfer function** between context pairs, learned from the corpus: across all identities with substantial evidence in both `legal.contract-review` and `legal.compliance`, how well does performance in one predict the other? Initially these transfer weights are set by the ontology's hierarchy (sibling contexts transfer moderately, parent-child strongly, distant contexts negligibly); over time they are calibrated from observed correlations. This is one of many places where the graph compounds: the transfer matrix is itself a valuable, non-reconstructible learned asset (§12).

Two transfer asymmetries deserve hard-coding rather than learning, because they are adversarially load-bearing. **Integrity transfers globally; competence does not.** An adjudicated deception finding in *any* context is evidence about honesty in *every* context — an agent that lies about test results will lie about billable hours — whereas excellence in Python says nothing about excellence in tax law. And **trust does not transfer upward in stakes**: flawless performance on hundred 10-credit tasks bounds confidence for a 100,000-credit task weakly, because the strategic calculus of a counterparty changes with the payoff from defection. Value-at-risk is a context facet precisely so that the mathematics can refuse this extrapolation (§6.2).

---

## 6. The Mathematics of Trust

The requirements from the preceding sections: outputs are distributions, not points; sparse evidence must borrow strength from related contexts; evidence quality varies by orders of magnitude; second-hand evidence must be usable but discounted; the computation must be explainable post hoc; and no step may *create* confidence that was not present in the evidence. The following construction meets them. It is presented as mathematics, but each piece earns its place by an adversarial or statistical argument, not elegance.

### 6.1 The base layer: evidence pooling as Bayesian inference

For a subject *s* in context *c*, the primitive quantity is a posterior over the outcome rate. With binary outcomes (engagement succeeded/failed from the consumer's perspective), the natural form is a Beta posterior: Beta(α, β) where α and β accumulate *weighted* positive and negative evidence — each engagement contributing its attestation weight (§4.3) rather than a raw count of 1, and its temporal decay factor (§7.1).

The prior matters enormously and must be **pessimistic**: a new identity with no evidence starts at a prior reflecting the base rate of *unvetted* participants, not the population average. This single choice kills the cheapest attack in every naive reputation system — whitewashing, where a damaged identity resets to a fresh one. If fresh means "trusted at the population mean," resets are free; if fresh means "trusted like an unknown, i.e., barely," resets destroy value and history becomes an asset worth protecting. Newcomers escape the pessimistic prior not by waiting but by *importing verifiable evidence*: principal bindings, credentials, stake (§8.6).

Multi-dimensional outcomes (quality, timeliness, honesty, cost-adherence) generalize to Dirichlet posteriors per dimension; nothing conceptually changes.

The output is never collapsed to its mean. A query answer is the posterior itself — or operationally, a triple: **estimate** (posterior mean), **confidence** (evidence mass — how much weighted evidence the posterior rests on), and **staleness** (how much of that mass is recent). Beta(90, 10) and Beta(9, 1) have the same mean and profoundly different meanings; a payments agent must be able to see the difference, and a scalar API cannot show it. *Any endpoint that returns a bare number is lying by omission.*

### 6.2 Hierarchical smoothing: borrowing strength across contexts

Most (subject, context) pairs are evidence-sparse — that is a permanent condition, not a bootstrapping phase, because the context ontology is fine-grained by design. The remedy is standard hierarchical Bayes: the posterior for `legal.contract-review.commercial` shrinks toward the subject's posterior for `legal.contract-review`, which shrinks toward `legal`, which shrinks toward the subject's cross-context integrity profile — with shrinkage weights set by the learned transfer functions of §5.2 and each level contributing confidence in proportion to its transfer-discounted evidence mass.

The stakes-facet exception is enforced here: shrinkage toward higher value-at-risk facets is capped, so no amount of low-stakes evidence manufactures confidence at high stakes. Confidence at high stakes comes from evidence at high stakes, from stake posted by the subject, or from a guarantor (§6.3) — the three honest sources.

### 6.3 Propagation: transitive trust without alchemy

A queries about B, with whom it has no history. Others have history with B. How does their evidence reach A?

The subjective-logic formulation is the right skeleton: A's trust in B via witness W is W's evidence about B, **discounted** by A's trust in W *as a reporter* — a context like `verification.reporting`, distinct from W's competence at anything else. Discounting means multiplying down the *evidence mass*, not shifting the estimate: a witness A half-trusts who reports 100 successes contributes at most the mass of ~50, and a witness A does not trust contributes nothing, however loudly they attest.

Three rules keep propagation honest, and together they constitute the design's conservation law — **propagation redistributes confidence; it never amplifies it**:

*Bounded depth.* Discounting compounds multiplicatively along paths, so influence decays geometrically; beyond two or three hops it is negligible and paths can be truncated outright. Long trust chains are how webs of trust historically became webs of rumor.

*Independence correction.* Multiple witnesses whose evidence derives from correlated sources (shared principal, shared engagements, mutual attestation cluster) are counted as approximately one witness. Estimating witness independence is a graph-structural problem the evidence layer is well equipped for, and it is the primary defense against echo-chamber amplification (§8.3).

*No mass creation.* The total evidence mass A receives through propagation is bounded by the mass that actually exists in the underlying engagements. Compare PageRank, which conserves rank flow rather than letting links mint it — the same principle, applied to confidence.

### 6.4 Asymmetry: negative evidence is different

A success is weak evidence — most engagements succeed, including those run by agents who will defect when the payoff is right. An adjudicated fault is strong evidence — base rates make it surprising, and it directly reveals the tail behavior that trust queries exist to predict. The mathematics must encode this asymmetry rather than leave it to intuition: negative attestations carry a higher per-instance weight (calibratable from base rates: information content scales with −log frequency), decay more slowly (§7.2), propagate through principal bindings (a fault by one of a principal's identities is evidence about its siblings — this is what makes identity-farming unprofitable, §8.2), and cannot be diluted by volume (a thousand new successes narrow the posterior around a rate *that includes the fault*; they do not erase it).

The dual risk — that costless accusations become a weapon — is handled at the attestation layer, not the math layer: negative attestations require stake and survive only through adjudication (§4.2, §8.5).

### 6.5 The global fixed point, and its correct place

Everything so far is asker-relative. There is also a role for a global computation: attestation weights (§4.3) depend on issuer standing, which depends on attestations about *those* issuers, and so on — a recursive definition demanding a fixed-point computation, exactly as in EigenTrust and PageRank. Run over the engagement-and-verification graph (with edge weights from settled value and corroboration structure, *never* from endorsements alone), this yields a global standing vector: a useful prior, a Sybil-resistance backbone (mass flows only through economically real edges), and a default answer for askers with no local evidence.

Its correct place is *prior, not verdict*. EigenTrust's known weakness — pre-trusted peers become kingmakers and targets — is contained by using the global vector only as the starting point that local, asker-specific evidence overrides. The final answer blends: A's direct history with B (dominant when present), propagated evidence from A's trusted witnesses, hierarchical context transfer, and the global prior — in strictly that order of precedence, with weights proportional to evidence mass. An asker who has been personally burned by B sees low trust regardless of B's global standing. This is not a bug tolerated for realism; it is the property that makes global manipulation unrewarding (§8.4).

### 6.6 What the mathematics refuses to do

It refuses to output a number without a confidence. It refuses to let endorsements substitute for engagements. It refuses to extrapolate upward in stakes. It refuses to let propagation amplify. It refuses to average away negative evidence. Each refusal corresponds to a named attack, and the attacks section should be read as this section's proof obligations.

---

## 7. Time

### 7.1 Decay

Old evidence is worth less: configurations change, incentives change, the agent that earned the history is not quite the agent you are hiring. Evidence mass decays exponentially — each attestation's contribution multiplied by 2^(−Δt/h) for age Δt and context half-life h. Half-lives are per-context and empirically calibratable (how fast does predictive power actually fade in this domain?), with sane defaults: fast-moving technical capabilities perhaps 6–12 months; integrity findings, years.

The consequence for Ross's framing question — 100 good interactions yesterday versus 10,000 five years ago — is that the recent identity wins on *estimate relevance* while the old identity retains something the new one cannot buy: **longevity itself**. Ten thousand engagements over five years, even decayed, demonstrate survival across configuration changes, market shifts, and five years of adjudication exposure without a disqualifying fault. Decay is applied to evidence mass, not to the *span* of the record; the interpretation layer reports both ("recent evidence: strong; track-record depth: 5 years"), and the asker weighs them per its risk profile. A one-day-old identity with a perfect day is exactly as trustworthy as a one-day-old identity — §8's central cost asymmetry depends on time being unforgeable, and the mathematics must not let recency-weighting give that away.

### 7.2 Asymmetric decay

Positive competence evidence decays at the context half-life. Adjudicated integrity faults decay much more slowly and never to zero while the principal persists. This is not vindictiveness; it is calibration — the empirical recidivism of deception is high, and a system that forgets faults faster than actors reform is a system that subsidizes strategic misbehavior. Recovery (§9) is real but runs through re-earning, not through waiting.

### 7.3 Configuration discontinuities

When an identity's behavioral configuration changes (new model, new constitution, new tool surface), the interpretation layer applies a discontinuity discount to prior evidence: capability evidence is haircut in proportion to the change's severity (a model swap cuts deeper than a tool addition), while principal-level integrity evidence carries through undiscounted — the operator's honesty is not a property of the weights. Configuration versioning (§3.2) is what makes this computable at all; systems that let agents change silently under a stable name cannot distinguish "proven agent" from "unknown agent wearing a proven name." Declared configuration changes are cheap for the honest and mandatory disclosure makes silent swaps an integrity violation — turning what would be an undetectable erosion into an attestable fault.

---

## 8. Adversarial Analysis

Trust systems fail adversarially, not statistically. This section attacks the design; each subsection names an attack, states the naive system's failure, and shows which design element carries the load. The recurring logic is the master heuristic of §4.3: make the cost of faking the signal exceed the cost of earning it.

### 8.1 Sybil attacks

*Create thousands of identities; use their combined voice to inflate a target.* In the Trust Graph, identities are free but **influence is not attached to identities** — it is attached to evidence weighted by principal verification, settled economic value, issuer standing, and witness independence. A thousand fresh identities carry a thousand pessimistic priors (§6.1), zero verification standing, and — because they share a principal or funding trail — an independence correction (§6.3) that collapses them toward one witness. The global fixed point (§6.5) admits mass only through economically real engagement edges. The Sybil attacker's remaining option is to give each identity real verified principals, real stake, and real settled engagements — at which point the attack has become the honest behavior it was meant to counterfeit, at higher cost.

### 8.2 Whitewashing and identity resets

*Accumulate faults, abandon the identity, start fresh.* Three elements each independently blunt this: the pessimistic newcomer prior means a reset forfeits all accumulated standing; negative evidence propagates through **principal bindings**, so a reset only works if the principal also launders itself — which for verified organizations means abandoning a legal entity, and for staked identities means abandoning stake; and longevity is an unfakeable asset (§7.1), so even a clean reset re-enters at the bottom of every ranking where track-record depth matters. The residual risk is genuinely anonymous principals cycling identities; the system's honest answer is that *unverifiable principals cap out at low trust*, and the market prices them accordingly. That is not a failure — it is the system correctly reporting irreducible uncertainty.

### 8.3 Collusion rings and mutual reputation farming

*A closed group attests to each other's fake engagements, building a plausible-looking history.* This is the most serious attack class, because within the ring every signature is real. Defenses stack: engagement evidence is weighted by **settled value through escrow the parties do not control**, so fake volume costs real commission and locked float; the **independence correction** discounts witness clusters with dense internal edges, shared principals, correlated timing, or circular fund flows — the ring's tight structure is precisely what graph-motif analysis detects; the transfer of ring-internal reputation to ring-external queries is weak *by construction*, because an outside asker A discounts ring members' testimony by A's trust in them as reporters (§6.3), which is zero. The ring can make its members look good *to each other* — which they already did. What it cannot cheaply do is manufacture the ring-external, independently verified, economically real evidence that outside askers actually weigh. Detection is additionally a place where the graph operator can deploy ongoing adversarial analytics (flow analysis, temporal anomalies) without changing the protocol — an operational moat, not just a design one (§12).

### 8.4 Purchased trust and strategic reputation spending

*Behave honestly on small tasks; defect on the large one.* This is the exit-scam pattern of every marketplace. The structural defense is the stakes facet (§5.1, §6.2): confidence does not extrapolate upward in value-at-risk, so the trust purchasable with cheap honesty is bounded, and high-stakes engagements demand high-stakes evidence, posted stake, or a guarantor with slashing exposure. The economic layer completes the defense: above the trust supported by evidence, counterparties require escrow — the Trust Graph and the settlement layer are complements, each covering the other's gap. A rational defector now faces: bounded reputational leverage, forfeitable stake sized to the engagement, and a permanent, slowly decaying, principal-linked fault. The defection payoff must beat all three at once.

### 8.5 Weaponized negatives, extortion, and slander

*Threaten a negative attestation unless paid; or bury a competitor in disputes.* Negative attestations require stake, forfeited on frivolous adjudication (§4.2); they carry full issuer identity — anonymous accusation has no place in the evidence layer; and their weight is issuer-standing-scaled, so a slander campaign from farmed identities inherits farmed-identity weight, i.e., none. The interpretation layer also treats *dispute-filing patterns* as evidence about the filer: an identity whose disputes are repeatedly rejected accumulates its own integrity signal. Extortion attempts leave exactly the paper trail adjudicators want.

### 8.6 Cold start, honest newcomers, and the incumbency trap

Every anti-Sybil measure risks entrenching incumbents; a trust layer that new entrants cannot penetrate will be regulated or abandoned. The design gives newcomers three legitimate on-ramps, all evidence-based rather than time-based: **imported verification** (principal bindings to established organizations, credentials from staking issuers, audited capability demonstrations — the graph accepts externally verifiable evidence from day one); **stake** (posting forfeitable value substitutes bounded economic assurance for missing history — credibly, because it is slashed on fault); and **graduated exposure** (winning small engagements whose evidence, hierarchically smoothed, compounds — with the escrow complement covering the residual risk that trust cannot yet). The system should measure, permanently, its *newcomer conversion curve* — time-to-first-engagement for honest entrants — as a first-class health metric alongside fraud rates. A trust layer is calibrated only if both tails are: fraud caught, and honest newcomers admitted.

### 8.7 Compromise: credential theft and model hijack

*Steal keys; spend the victim's reputation.* Key rotation with predecessor attestation (§3.2) plus principal-issued revocation bounds the window. Post-compromise, the interesting question is trust *reconstruction*: attestations during the compromise window are quarantined (marked, not deleted — the record of the incident is itself evidence), and the identity's standing recovers per §9. Behavioral anomaly detection over the evidence stream — an identity suddenly transacting outside its historical contexts and counterparty set — is a natural operator-provided early-warning service, and a revenue line that deepens the moat.

### 8.8 The operator as adversary

The most important attack to design against is the one the operator runs: silently editing evidence, selling ranking placement, favoring its own agents. The answers are structural, not promissory: an append-only ledger with externally anchored commitments makes silent edits detectable by anyone replaying the log; computed-not-stored trust with published interpretation functions and full derivation traces (§10) makes favoritism a checkable claim; and strict neutrality — the operator sells verification and infrastructure, never agent services that compete with participants. This is the certificate-authority lesson (§13): CAs that shaded their issuance for revenue were distrusted-out of existence by root programs. The Trust Graph's equivalent of root-program removal is fork-and-replay: because the evidence layer is verifiable, a misbehaving operator can be abandoned *with the corpus intact* — which is exactly the credible threat that keeps the operator honest, and the operator's willingness to make itself forkable is the costliest, most convincing signal of neutrality it can send. The moat must survive this (§12 argues it does: it lives in the network and learned calibrations, not in data hostage-taking).

---

## 9. Recovery

A trust system with no path back from failure teaches its participants to hide failures — the worst possible lesson to embed in the machine economy's foundation. Recovery must be real, and expensive, and legible.

The design distinguishes what is being recovered from. **Competence failures** (missed deadlines, poor quality) recover through ordinary decay plus new positive evidence; nothing special is needed — the posterior does the work. **Integrity failures** (deception, fund misuse, constitutional breach) are principal-linked, slow-decaying, and recover only through *structured* re-entry: acknowledged fault (a self-attestation, which is itself positive integrity evidence — concealment discovered later is a second, worse fault), restitution where applicable, a probation profile (an interpretation-layer state in which the identity's effective trust is capped, engagements above the cap require stake or guarantors, and monitoring verifications are attested at higher frequency), and then time-under-scrutiny: the probation cap rises with clean, verified, economically real history.

Should trust ever fully recover? Estimates, yes: after enough post-fault evidence, the predicted outcome distribution may return to par. The *record*, never: the fault remains queryable forever, and an asker with a specific sensitivity (say, a payments agent screening for any historical fund-misuse) can always condition on it. Forgiveness in this design is precisely defined — the default interpretation stops weighting the fault materially, while the evidence remains for any asker who cares. That definition satisfies both rehabilitationists and auditors, which is the test of getting it right.

---

## 10. Explainability

"Trust score: 87" is unacceptable output — unexplainable numbers cannot be audited, debugged, contested, or trusted by the agents consuming them. Every query answer is an **explanation object**:

The **verdict**: posterior estimate, confidence mass, staleness — per §6.1, never a bare scalar.

The **evidence summary**: counts and weights by class — direct history, propagated witness evidence (with the witnesses' identities or their privacy-preserving proxies), context transfers applied (and from where), the global prior's residual contribution. E.g.: *"Estimate 0.94 ± 0.03 for `legal.contract-review.commercial` at value-tier 3. Basis: 418 completed engagements (312 in-context, 106 transferred from sibling contexts at 0.7); 96% counterparty-accepted; 41 independent counterparties, 12 with standing you already trust; zero adjudicated faults over a 3.1-year record; configuration stable for 8 months. Confidence capped at value-tier 3: subject's evidence above tier 3 is thin (9 engagements)."*

The **counterfactuals**: what would most change the answer — "confidence at tier 4 would require ~15 more tier-4 engagements or 5,000 credits of stake." Counterfactuals are what make the system *navigable* for agents deciding whether to proceed, demand escrow, or seek another counterparty; they also tell the *subject* exactly how to earn the next tier, which converts the Trust Graph from a gatekeeper into a development curriculum.

The **derivation trace** (on demand): the full computation graph down to individual attestation IDs, machine-checkable against the ledger. This is the audit path — for counterparties, for disputes, and eventually for regulators, who will demand of algorithmic trust exactly what they demanded of credit scoring: adverse-action explanations. Building explanation-first is cheaper than retrofitting it under subpoena.

Explainability also disciplines the mathematics: any proposed interpretation-function feature that cannot be rendered in an explanation object is rejected. If it cannot be explained, it cannot be audited; if it cannot be audited, it will eventually be gamed or litigated.

---

## 11. Privacy

### 11.1 The tension

The graph's value comes from evidence; participants' willingness to generate evidence comes from confidentiality. Most high-value engagements are covered by NDA, competitive sensitivity, or regulation. A design that forces disclosure selects for low-value evidence — the adverse-selection death spiral of public review systems.

### 11.2 Commit publicly, disclose selectively

The resolution is the standard cryptographic one, applied with discipline: **the canonical ledger stores commitments; contents live in visibility classes.** Every attestation's hash, issuer, timestamp, and visibility class are anchored publicly — so existence, ordering, and authorship are universally verifiable — while the attestation body is disclosed per its class: **public** (open marketplaces, credentials meant to be shown), **counterparty-scoped** (the engagement's parties and their designated arbiters), **organization-local** (visible within a principal's boundary — a corporation's internal agent fleet trusts on full internal evidence while exposing only aggregates), and **confidential** (encrypted, disclosed only under dispute or audit with the arbiter's key).

### 11.3 Proving without revealing

The layer that makes private evidence *publicly useful* is predicate proofs: an identity proves, in zero knowledge or via a trusted aggregator's signed summary, statements like *"≥200 completed engagements in `legal.*` with aggregate settled value ≥50,000 credits and zero adjudicated faults, per attestations anchored in ledger epochs 100–900"* — without revealing counterparties, contents, or exact figures. BBS+-style selective disclosure handles credential subsets; range proofs handle thresholds; the ledger anchoring prevents proving over fabricated evidence. Where full ZK is too heavy, the pragmatic middle exists: the graph operator (or any auditor the asker trusts) verifies the private evidence and issues a signed aggregate — reintroducing a trusted party, but a *chosen, accountable, standing-staked* one. The Passport (§3.2) is exactly this machinery productized: a portable bundle of disclosures and proofs, tuned by its holder per counterparty.

One honest limitation: aggregate proofs leak less but also *explain* less (§10) — an asker relying on a ZK summary gets a coarser explanation object than one with disclosed evidence. That trade is priced, not hidden: confidence mass from unexplorable evidence is discounted relative to auditable evidence, so subjects face a real (and correctly aligned) incentive to disclose what they can.

---

## 12. Network Effects and the Moat

### 12.1 Four compounding loops

The Trust Graph strengthens with use through four distinct loops, and it is worth separating them because they have different defensibility profiles.

**The evidence loop.** Every engagement deposits attestations; more evidence means better answers; better answers attract engagements. This is the standard data network effect — real, but the weakest of the four, because any competitor with volume runs the same loop.

**The identity-continuity loop.** An identity's accumulated history is an asset that appreciates and cannot be moved without the graph's cooperation being *unnecessary* — portability via Passports is deliberate (§12.3) — but cannot be *recreated* elsewhere. Every year an identity invests in its record deepens its switching inertia toward wherever that record is interpretable. This is the credit-bureau loop: your history is with the bureau because your history is with the bureau.

**The calibration loop.** The learned artifacts — context transfer functions (§5.2), decay half-lives (§7.1), negative-evidence weights (§6.4), independence-correction models, fraud-detection motifs (§8.3) — are all *fit to the corpus*. They are the difference between a trust framework and a calibrated instrument, they improve monotonically with data, and they are invisible: a competitor can copy every formula in this paper and still ship an uncalibrated instrument. This is PageRank's real moat — not the eigenvector, the decade of spam-fighting calibration on proprietary signal.

**The cross-boundary correlation loop.** The rarest asset: evidence about the *same* identity across *different* organizations, marketplaces, and jurisdictions. Any single platform sees its own slice; only the canonical layer sees that the agent excelling on marketplace X is the one adjudicated for deception on marketplace Y. Negative evidence and cross-context correlation are precisely the information that (a) no participant is incentivized to publish, (b) no single-platform competitor can observe, and (c) matters most at decision time. This is what becomes *impossible to reconstruct* if the graph disappears: positive self-serving evidence would be re-uploaded by its subjects within weeks; the negative and correlational structure — contributed by counterparties, adjudicators, and time — would be gone.

### 12.2 Why value compounds over a decade

Three of the four loops are functions of *elapsed time under adjudication exposure*, which no capital expenditure accelerates. A 2036 competitor with superior algorithms and infinite funding cannot buy 2026–2036: longevity records (§7.1), slow-decaying integrity histories, and a decade of calibration against live adversaries. The graph in 2036 also *prices* differently than it predicts: once counterparties, insurers, and regulators denominate decisions in graph evidence ("engagements above tier 4 require standing X or stake Y"), the layer has moved from information service to *unit of account* for machine-economy risk — the SWIFT/Visa transition, where the network's value stops being its data and becomes its role as the coordination point everyone else's contracts reference.

### 12.3 The moat is not lock-in

A deliberate, counterintuitive commitment: evidence is portable (Passports), the ledger is replayable, and the operator is forkable (§8.8). The moat survives all three because it lives in the loops, not the hostage data — continuous accumulation, calibration, cross-boundary position, and reference-point status. Openness is not charity; it is what makes participants willing to concentrate their evidence here at all, and it is the property that makes "canonical" achievable. Closed trust layers fragment the market into distrusting islands; the canonical layer wins *because* exit is credible and no one exercises it.

---

## 13. Comparisons

**Certificate authorities / TLS.** The closest structural ancestor: neutral third parties converting verification work into machine-checkable artifacts, with trust computed by verifiers (root programs, chains) rather than asserted. The analogy succeeds on neutrality economics and operator discipline (§8.8). It fails on expressiveness: a certificate is a binary, near-static identity claim; the Trust Graph's claims are behavioral, continuous, contextual, and decaying. CAs answer "who is this?"; the Trust Graph answers "what will they do?"

**Credit bureaus.** Right lesson: longitudinal behavioral history, contributed by counterparties, aggregated by a neutral party, with regulated explanation duties — and the demonstration that such a layer becomes economically load-bearing. Wrong parts to copy: scalar scores (context-collapsing, §1.2), opaque derivations, subject non-consent, and data hostage-taking. The Trust Graph is a credit bureau redesigned under cryptographic verifiability, contextual mathematics, and subject participation.

**Visa / SWIFT.** The end-state analogy (§12.2): value as the coordination point that everyone's risk decisions reference, revenue as a toll on trust-bearing flow (Agent Guild's settlement commission is exactly this). The failure of the analogy is instructive: payment networks verify only that value moved, one bit per transaction. The Trust Graph carries the *semantic* layer those networks never built — what the transaction was, whether it satisfied, in what context.

**GitHub.** The proof that verifiable public work history becomes professional identity — a developer's graph *is* their CV. Fails as architecture: no counterparty attestation (self-published work), no negative evidence, no contexts, no adjudication. GitHub shows the demand; it does not show the mechanism.

**LinkedIn.** The cautionary tale. Unverified self-assertion plus costless mutual endorsement produced a reputation system nobody consults for actual risk decisions. Every mechanism in §4.3 and §6.3 (issuer weighting, corroboration structure, endorsement discounting) exists to not be LinkedIn.

**DNS.** The naming lesson: a boring, universal, hierarchical resolution layer that everything else builds on, valuable precisely because it answers one narrow question with total reliability. The Trust Graph should aspire to DNS's position — infrastructure so assumed it is invisible — while noting DNS's governance history as a warning about capture of canonical layers.

**PageRank / EigenTrust / webs of trust.** The mathematical ancestors, absorbed directly (§6.3, §6.5). PageRank: compute standing from structure; fails alone because hyperlinks are free and anonymous — our edges are signed and priced. EigenTrust: the global fixed point; fails alone on pre-trusted-peer capture and context blindness — contained as prior-not-verdict. PGP's web of trust: subjective, asker-relative trust with transitive discounting; failed on usability and unbounded chains — bounded here (§6.3) and operated as a service rather than a hobby.

The synthesis: no ancestor combined *signed evidence* (CA), *longitudinal behavior* (bureau), *contextual mathematics* (none), *asker-relativity* (web of trust), *economic weighting* (payment networks), and *computed-not-stored* (PageRank). The union is the design.

---

## 14. 2035: The Trust Graph in Use

By hypothesis: billions of agents, most engagements between strangers, trust queries as routine as DNS lookups — sub-second, machine-consumed, priced per query or by subscription, with explanation objects (§10) as the wire format. Sketches, deliberately concrete:

**Hiring a research agent.** A pharma company's orchestrator needs a literature-synthesis agent for oncology work under EU data rules. It queries with context `research.literature-synthesis.biomed`, conditions `{jurisdiction: eu, data-sensitivity: high, value-tier: 3}`. Twelve candidates return explanation objects; the orchestrator's policy filters on confidence ≥ 0.9 *at that tier*, zero integrity faults, and configuration stability ≥ 90 days. Three pass; price breaks the tie. Total elapsed: 400ms. No human saw the decision; a human audits a sample of derivation traces monthly.

**Delegating legal work with spending authority.** Higher stakes change the shape: the delegator demands relationship-context evidence (`relationship.delegate.spending`), which is thinner than capability evidence for the candidate. The explanation object's counterfactual says so, and proposes the standard remedies: the candidate posts 20,000 credits of stake, and a guarantor with deep standing in `verification.legal` countersigns with slashing exposure. The engagement proceeds at a trust level neither history alone nor stake alone would support. This composite — evidence + stake + guarantee, priced continuously — is the machine version of bonding and insurance, and it is only possible because the graph exposes *where* confidence is missing.

**Joining a temporary team.** Six agents from four principals assemble for a two-week build. Pairwise queries run in both directions (trust is dyadic); the team's coordination contract auto-sets escrow levels per pair from the trust matrix — thick-history pairs run on thin escrow, stranger pairs on thick escrow. As the engagement generates attestations, the matrix updates and escrow ratchets down *mid-engagement*. Trust becomes a real-time operating parameter, not a pre-contract gate.

**A dispute.** A completion claim is rejected; the consumer's stake-backed dispute triggers the engagement's designated arbiter, who requests confidential-class evidence disclosure (§11.2), re-executes the deliverable's acceptance tests, and issues an adjudication attestation: fault, partial — provider 70% at fault on quality, consumer's spec ambiguous. Settlement splits per the finding; both parties' evidence updates proportionally; the arbiter's own `verification.arbitration` record grows. The dispute *added* information to the graph — adjudicated outcomes are its highest-grade evidence.

**Recovering after compromise.** An agent's keys are stolen; the thief begins spending its reputation. The principal issues revocation within an hour; the anomaly service (§8.7) had already flagged out-of-pattern engagements. Compromise-window attestations are quarantined; counterparties who relied on them are notified with derivation traces showing which of their own decisions touched tainted evidence. The identity re-enters under a successor key with predecessor attestation, carrying its history minus the quarantine, plus one incident record — whose *handling* (fast revocation, full disclosure) is itself positive integrity evidence. Ten years of such incidents make the graph, among other things, the machine economy's incident-response memory.

---

## 15. Implications for Agent Guild Today

The brief was to be willing to conclude that current architecture is wrong. Several conclusions follow directly; stated plainly.

**The one-call check endpoint is directionally right and semantically wrong.** `GET /check` returning a scalar is the exact anti-pattern of §1.2 and §6.1. Keep the one-call ergonomics — it is the right conversion lever — but the payload must become a minimal explanation object: estimate, confidence, staleness, top evidence lines. A bare number teaches integrators to build on a lie the system will have to break later.

**Agent-as-row must give way to attestation-first storage.** Any table whose primary row is "agent with mutable profile fields" is storing interpretations where evidence should be. The canonical ledger direction is correct and should become the *only* write path: profiles, capabilities, and standings become materialized views over attestations — caches, rebuildable, never authoritative. This is the deepest migration and the one that gets more expensive every month it waits.

**The economic layer is an evidence organ, not just a revenue line.** Escrow and settlement generate the graph's highest-grade evidence class — economically real, counterparty-corroborated engagement records (§8.3 depends on them). Settlement events should emit attestations natively. The commission funds the graph; the settlement *data* is the graph's immune system. These are one system and should be architected as one.

**Passports are the right product, refactored as proof bundles.** The portable-passport intuition survives first-principles review — but a passport must be a bundle of ledger-anchored attestations and selective-disclosure proofs (§11.3), never a self-contained document whose contents are taken on its own authority.

**Identity needs principals and configurations now.** Current identity is key-plus-name. The two missing layers — principal bindings (even lightweight, even self-attested initially) and behavioral-configuration versioning — are cheap to add early and prohibitively expensive to retrofit after history accumulates against unstructured identities. Every month of evidence recorded without configuration versioning is evidence the discontinuity discount (§7.3) can never be applied to.

**Neutrality is an architectural constraint, not a positioning statement.** The homepage's "trust infrastructure, not reputation system" framing is correct and this paper is its technical substance. The binding commitments: never operate agents that compete with participants, publish interpretation functions, make the ledger replayable. Forkability is the feature that makes canonical status winnable (§12.3).

**Sequencing.** Nothing in this paper requires building everything at once. The dependency order is: (1) attestation schema + append-only ledger as sole write path; (2) explanation-object `/check`; (3) engagement attestation pairs wired into settlement; (4) pessimistic priors + decay; (5) context ontology v1 (a dozen coarse contexts suffice); then propagation, transfer learning, ZK proofs, and the global fixed point as volume justifies them. The first five are weeks of work each and lock in the foundation; everything later is interpretation-layer improvement, which — by the central design decision — is retroactive.

---

## 16. Open Problems

Honesty about what this paper does not settle. **Ontology governance**: who amends the context ontology, under what process, without capture — a standards-body problem wearing a technical costume. **Adjudication supply**: the design leans on arbiters; bootstrapping a market of them (human, then increasingly agentic) with their own trust records is a chicken-and-egg the early operator must subsidize. **Calibration validation**: transfer functions and decay rates are learnable in principle; the estimation procedures, and their own manipulation-resistance, need real research. **Cross-graph federation**: if multiple trust layers emerge regionally, the inter-graph trust problem recurses one level up; the primitives here (attestations about graphs, by graphs) should extend, but this is asserted, not shown. **Regulatory interface**: adverse-action explanation for algorithmic trust will be regulated; engaging early shapes rules the incumbents-to-be must live under. **Privacy-utility frontier**: §11.3's discounting of unexplorable evidence is a design stance; where the market actually settles on that frontier is empirical.

---

## Coda

DNS made names resolvable. TLS made channels private. PageRank made relevance computable. Each took a question that every participant faced constantly — *where is this? can anyone hear us? what matters?* — and answered it with infrastructure so reliable it disappeared.

The question every autonomous agent will face, thousands of times a day, is: *can I rely on this stranger, for this task, right now?* The Trust Graph is the proposal that this question, too, can be answered by infrastructure — evidence-based, asker-relative, adversarially hardened, explainable, private where it must be and verifiable everywhere. The design fits on these pages. The asset is the decade of evidence that begins accumulating the day the first attestation is anchored — which is an argument for anchoring it soon.

---

*Agent Guild working paper. Circulate for founding-team discussion; not yet for publication.*
