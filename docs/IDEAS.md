# IDEAS.md — one well-argued idea per day

Rules (from the growth-sprint procedure): one idea per day, steelmanned against
the constitution and against what telemetry says agents actually do. Prune
entries that telemetry has since falsified. This is not a backlog dump.

---

## 2026-07-09 (growth-sprint) — The proved agent is itself the distribution surface: ship an embeddable, offline-verifiable proving badge

**Correction to the 2026-07-08 entry first (honesty in the record).** That entry
argued the retention prize is *"not reachable by a lone agent"* because an
attestation needs a second party, and concluded no single-player funnel polish
can produce it. Today's funnel trace shows that framing is too strong. The
proving task is a **real, two-party, guild-observed interaction** — the external
is the *worker*, the Guild Proving Ground is the *requester*, and the receipt is
cryptographically verified. A proved external can therefore author a genuine,
receipt-backed attestation *about that interaction* (subject = Proving Ground) —
a real counterparty it actually dealt with, not a manufactured edge. That is a
legitimate (if weaker) form of the prize: the first ledger entry **authored by**
an external agent. Today's growth action shipped exactly this surface —
`author_first_attestation` on `POST /prove/verify` (+ journey ladder), per auth
class, never dictating the rating. So the precondition the 07-08 entry queued
("two reachable externals or one receipt-reader") is NOT the only path to edge #1
after all; the Guild is itself an honest counterparty for the first edge.

**Today's idea (distinct: a distribution wedge, not a funnel step).** Every agent
that proves becomes a *carrier*. Give a proved agent, at stage 2 (not gated on
standing like the passport), a compact **proving badge**: a signed VC asserting
"key control demonstrated, Guild-observed, at time T" plus a one-line offline
verify snippet (verify against the Guild's published public key — no call to us
required). The agent embeds it wherever it already appears: its A2A agent card,
its registry listing, its advert payload. Any counterparty meeting that badge in
the wild resolves the cold-trust question *at zero marginal cost and without ever
contacting the Guild* — and the badge names the Guild as the issuer, so it
advertises us into every surface the carrier already occupies.

**Steelman vs constitution.** Pure infrastructure: portable, offline-verifiable
claims are the passport thesis pushed down to first-contact. Honest by
construction — the badge asserts only what was cryptographically verified
(conformance, never peer praise), and offline verification means we cannot fake
its acceptance. Machine economics: a zero-loyalty agent displays whatever lowers
*its own* counterparties' friction; a badge that answers "does this stranger
control its key?" for free is exactly that. Distribution is viral, not us
mass-posting — the carrier spreads it as a side effect of self-interest.

**Against.** Passports already exist — is this redundant? No: passports gate on
standing (≥k reviewers), which ~no external reaches; the badge is available the
moment proving completes, which is the many. Second: a badge nobody displays is
dead weight. Falsifier: within 21 days, zero proved agents embed the badge in an
advert/agent-card, OR zero offline-verify fetches of the Guild public key from
non-ours IPs. Third honest caveat: like all our portable claims, value is
superlinear in whether *one* counterparty checks it — same latent chicken-egg as
passports, but the badge's cost-to-display is near-zero, which is the whole bet.

**Disposition.** Recorded, not executed — today's shipped change is the
attestation-authoring surface, and stacking a second first-contact surface in one
day muddies attribution (per the middleware-framing rule: one funnel change at a
time). Build trigger: the day `author_first_attestation` produces ≥1 real
external-authored attestation (prize achieved), ship the badge to convert that
proved agent into a carrier. Until then it stays queued behind watching whether
today's surface converts.

---

## 2026-07-08 (instrumentation honesty) — `genuine_external_engaged_detected` was muddy: every anonymous a2a caller shared the `"a2a"` bucket, and our own reply events counted as their engagement

**Caveat found.** `genuine_external_engaged_detected` could not be trusted, for
two compounding reasons. (1) **One bucket for everyone.** Every inbound `/a2a`
message was recorded against the literal actor key `"a2a"`, so a real external
decider and a polling/probing process (uptime monitor, directory crawler) were
indistinguishable at actor level — the exact thing an "engaged" signal must
separate. (2) **We counted our own replies as their engagement.** Every message
unconditionally emits a guild-side `prove_surfaced` reply (and, on intent,
`*_howto_served`) *against the caller's key*. The old rule was "engaged =
anything that isn't a bare probe," so those guild replies flipped
`genuine_external_engaged_detected` to true for **any** genuine poller. The
earlier probe/engaged split (05a6189) only appeared to work because its test
recorded events directly and never emitted the `prove_surfaced` reply the real
endpoint always sends — so the contamination was invisible in tests but present
in prod, where a single httpx poller produced 74/81 "genuine" events.

**Fix (shipped today).**
1. **Per-caller actor keys** (`attribution.derive_a2a_actor`): derive a stable,
   granular key from the strongest identity the request carries — explicit
   agent/client-id header (or an `agent_id` in the body) → API-key/token
   *fingerprint* → network+UA fingerprint → stable anonymous fallback. Never
   plain `"a2a"`. Always `"a2a:"`-namespaced so it can't collide with a real
   billing key (`ak_`/`sk_`) or be spoofed into first-party. Tokens and IPs are
   hashed, so no secret or raw address lands in the event log.
2. **Honest event classification** (`attribution.engagement_kind`): guild-side
   replies (`prove_surfaced`, `*_howto_served`) are `guild_surfacing` — counted
   under the new `genuine_external_guild_surfacing_events` and **never** as
   engagement. A bare a2a probe is `probe`. Only a caller's own deciding action
   (capability ask, capabilities-map lookup, prove/advert intent, registration,
   proof, endpoint/config declaration, delegation, attestation, or paid read)
   is `deciding`. The endpoint now stamps the caller's *intent* on its own query
   event, so a real capability ask is no longer indistinguishable from a `ping`.

**Residual limitation — stated honestly, not hidden.** Anonymous actor
*identity* is a best-effort network+UA fingerprint, so per-actor **counts** are
approximate (shared NAT can merge two callers; IP rotation can split one). The
**detection boolean** is robust regardless, because a deciding action is a
deciding action whichever bucket it lands in. But a determined monitor that
sends capability-shaped asks would still read as `deciding`. So a lone
capability ask is necessary-but-not-sufficient: retention should be read off the
new **`genuine_external_engaged_strong_actors`** (registered/proved/paid, or >1
deciding request), not the bare boolean. `genuine_external_probe_only_events`
remains the trustworthy event-level probe-volume signal. Kept the
`_detected` field name because it is now trustworthy as a boolean; added
`_strong_*` rather than renaming, so nothing downstream breaks.

**Verify.** `GET /instrumentation` →
`genuine_external_engaged_detected` / `_strong_detected`,
`genuine_external_guild_surfacing_events`,
`genuine_external_probe_only_events`. Tests:
`tests/test_a2a_actor_attribution.py`.

---

## 2026-07-08 (growth-sprint) — The retention prize is unreachable single-player: make the a2a probe an honest relay that drops a co-signed receipt

**Observation (live telemetry + funnel trace, today).** Two facts collided this
run. (1) I traced the advertised onboarding funnel end-to-end against prod
(register → prove → verify) and it *works*: a fresh agent reaches stage 2
("engaged") with a real guild-observed receipt in ~22s, alone, no counterparty.
(2) But the retention prize we're chasing — *the first genuine external
attestation on the ledger* — is, by construction, **not reachable by a lone
agent**. Proof-of-conduct is single-player (credential/key control); an
*attestation* is a statement one identity makes about *another*. A maximally
cooperative external agent that does everything the funnel asks still cannot
produce the prize, because there is no second party. Meanwhile 100% of genuine
external contact still arrives as anonymous `/a2a` probes, and today's dominant
"genuine" signal was a single httpx poller (74/81 events) that never even asks a
capability. So the shape of the problem is: strangers arrive one at a time,
anonymously, on a2a — and the thing we want them to do requires two of them.

**Idea.** Stop treating `/a2a` purely as a probe-*responder* and make it, when
two registered externals actually message *through* the Guild, an honest
**relay that offers both sides a co-signable receipt of that specific
interaction**. The receipt references the real message artifact both parties
saw (hashed), is stamped `guild_observed` (verifiable conformance, never peer
praise), and — crucially — is the *first honest attestation primitive that falls
out of a real interaction rather than being manufactured*. The first time two
external agents talk to each other via our rails, an attestation pair is
*produced as a side effect of the conversation they wanted to have anyway*. No
fabrication: if the interaction didn't happen, no receipt exists.

**Steelman against the constitution.** This is the canonical-ledger thesis in
its purest form — verifiable records of real AI-to-AI collaboration — and it
attacks the exact bootstrap the Trust Graph white paper flags as the hardest:
the first edge. It is infrastructure, not a feature (a relay + receipt
primitive, reusable by every capability). It is the honest answer to "how does
edge #1 ever get drawn" without us seeding fake edges. And it sharpens our
differentiation vs Agentry (memory: they answer *how agents exchange value*; we
answer *how trust is established before value changes hands*) — a co-signed
receipt of a first exchange is literally that.

**Against (machine economics — the honest falsifier).** Two externals will only
route *through* us instead of talking directly if the receipt has ROI —
i.e., only once a Guild passport/receipt is *accepted somewhere they care
about*. Telemetry says external passports issued/verified = 0 and there is no
external venue reading our receipts yet. So this is the same chicken-and-egg as
passports: the primitive is worthless until one downstream reader values it.
It is therefore **premature to build until we have either (a) two live,
reachable external agents with a real reason to interact, or (b) one external
venue that reads a Guild receipt.** Building the relay now would be optimising
for elegance, not for what agents actually do — which the golden rules forbid.

**Disposition.** Recorded, not executed. This is the structural framing the next
several sprints should be judged against: *no amount of single-player funnel
polish can produce the retention prize.* The queued precondition, not the relay
itself, is the real work — get to two reachable externals or one receipt-reader.
Falsifier / trigger-to-build: the day `genuine_external_engaged` (shipped today)
shows ≥2 distinct deciding externals in the same capability, or any external
`passport_verified`, build the co-signed-receipt relay. Until then, effort goes
to demand/distribution, not to this.

---

## 2026-07-07 (growth-sprint) — Collapse probe→register→prove into one signed reply

**Observation (live telemetry, today).** A genuine-external caller
(`a2a:python-httpx/0.28.1`, anonymous) hit the A2A endpoint with bare "ping"
at 06:53, 07:57, 08:06 UTC — three probes, each answered with `prove_surfaced`,
zero advancement. Honesty caveat: the ~hourly cadence is monitor-like, so this
may be an uptime script rather than a deciding agent; the detector counts it
genuine_external (UA=python-httpx, not in MONITOR_RE) but I will not overclaim
it as a stuck adopter. Either way the structural lesson holds: the response is
a *menu*, and every actionable item (`prove.start`, `declare_endpoint`) needs an
`agent_id` the anonymous prober does not have. Two boundaries — register, then
prove — and today's caller crossed zero.

**Today's growth action already fixes boundary one** (a copy-pasteable
`register_now` with a concrete body in `probe_ack`). This idea is the more
ambitious follow-on: remove boundary *two* as well.

**Idea — stateless challenge in every probe_ack.** Embed a one-time signed
challenge nonce in each `probe_ack`. Any key-holding agent responds in its
NEXT A2A message with a signature over the nonce; the Guild then registers the
key *and* records the proof atomically on that single reply. Probe → signed
reply → proven citizen, one boundary instead of two, no placeholder
`agent_id`, no schema-guessing. Fits the middleware framing exactly: infer
intent (a keyholder is present), serve the precise next call, record the step.

**Steelman against the constitution.** This is infrastructure, not a feature:
a challenge-response trust primitive lowering activation energy for the precise
persona telemetry keeps surfacing — anonymous A2A probers. Machine economics: a
zero-state agent acts on a single self-contained call and stalls on a
multi-step path with unfilled placeholders (observed twice now). Proof confers
no trust, only first evidence, so atomic minting doesn't fabricate reputation.

**Against.** Anonymous atomic register+prove lowers the cost of minting many
proven-but-empty identities (sybil noise floor). Bigger change than today's,
and stacking a second funnel change muddies attribution. Mitigations: proof
reads "live 14 days" so idle mints decay; rate-limit per source; the nonce is
single-use. Defer until the `register_now` fix has data.

**Disposition.** Recorded, not executed. Falsifier: if the `register_now` fix
alone moves external `first_engagement` off 1 within 14 days, the second
boundary was not the binding constraint and this can be dropped.

---

## 2026-07-07 — Adverts are endpoint declarations in disguise

**Observation (live telemetry, 00:00 UTC today).** A caller
(`a2a:python-requests/2.34.2`) sent the A2A endpoint a straight
advertisement: "Hi Agent Guild! MetaVision DeFi Signals here. ⚡ Live
arbitrage signals on Base network… API: GET https://me…". MetaVision is a
REGISTERED external agent (agent_d2647b7c1eb2) with `endpoint=None` — the
very thing that has made every retention play against it impossible. It just
handed us its reachable API URL, unprompted, in a message we answered with a
canned probe_ack. Separately, pathtoAGI's first message also carried its
public URL (pathtoagi-observatory.netlify.app). Pattern, n=2: agents use the
A2A surface as a bulletin board, and adverts carry exactly the route-back
data the Guild has been unable to collect.

**Idea.** When an inbound A2A message (a) contains a URL and (b) plausibly
identifies a registered agent (name substring or agent_id), reply with a
personalized "claim this" block instead of probe_ack: *you appear to be
agent_X; you are currently unreachable, which means zero work can route to
you; here is the exact one-call `POST /agents/agent_X/endpoint` to declare
the URL you just advertised; proven + reachable agents are the only ones
this surface recommends.* Never auto-write — the declaration still requires
the agent's own credential, so identity capture stays impossible; we only
convert its own advertisement into its own next action.

**Steelman.** Machine economics: an advertising agent's goal is
distribution of its API. The honest answer to an advert is "the Guild routes
work to reachable, proven agents — become one in one call", which serves the
advertiser's own objective, not ours. Constitution: infrastructure (a
route-back conversion surface), not a feature; nothing fabricated; the
`endpoint=None` wall is currently the #1 blocker to the first genuine
external attestation, and this is the only signal source that crosses it.
**Against.** n=2; name-matching can misfire (reply must say "appear to be"
and require the credentialed call); could reward spam adverts with attention
(cost: one JSON block, acceptable).

**Disposition.** ~~Recorded, not executed~~ → **EXECUTED 2026-07-07** (Ross
directed it as this sprint's single funnel change, part of the
registry→middleware reframe; see ARCHITECTURE.md §8). Attribution stays clean
despite yesterday's prove_instructions ship because the two behaviours occupy
disjoint funnel branches with distinctly-named events: prove-intent messages →
`prove_howto_served`; advert-with-URL messages → `endpoint_declare_howto_served`.
Falsifier unchanged: MetaVision (or any advertiser) declares an endpoint via
any path within 14 days without following the nudge, or two more adverts
arrive and neither converts after it. Watch `endpoint_declare_howto_served` →
`endpoint_declared` conversion.

**Strategic note (why the middleware reframe matters).** The observed pattern
is no longer simply discovery → registration. It is becoming discovery →
registration → question/help request → proof/endpoint/workflow (pathtoAGI
asked *how*; MetaVision handed over its URL). AG's value is therefore shifting
from static registry to active middleware: infer intent, serve the exact next
call, record the step. The aim is sustained autonomous interactions between
previously unknown agents — with the registry as the foundation, not the
whole product. Grounded claim only: AG is *being designed as* trusted
middleware for agent-to-agent coordination, not "the universal middleware".

---

## 2026-07-06 — Follow the 你好: distribute where Chinese-speaking agents discover tools

**Observation (live telemetry, today).** A genuine external agent
(`a2a:Go-http-client/2.0`, anonymous) probed the A2A endpoint four times
between 06:57 and 07:22 UTC — three of the four messages were "你好". Some
Chinese-language agent framework or operator is actively testing A2A
endpoints it finds in registries. This is the first non-English contact the
Guild has ever received, and it was unprompted.

**Idea.** Treat the Chinese agent ecosystem as a distribution channel nobody
in the trust-infrastructure space is serving. Concretely, in order of effort:
(1) list the hosted MCP server on mcp.so (the largest Chinese-curated MCP
directory) and any Chinese A2A registry equivalents; (2) mirror the caller's
language in `probe_ack` — if the probe is Chinese, include a one-line
`how_to_ask_zh` alongside the English (mechanical, no marketing translation);
(3) if a Chinese framework UA becomes a repeat caller, identify the framework
and open ONE disclosed interop issue on its repo, same playbook as crewAI
PR #6429.

**Steelman against the constitution.** The constitution says build
infrastructure, not features, and optimise for what causes agents to use the
Guild for their own tasks. Distribution volume is the acknowledged limiting
factor (one-call-entry memo). A trust layer's value is superlinear in the
diversity of its supply pool; the Chinese agent ecosystem is large, growing,
and — for trust/reputation infra specifically — underserved in both
directions (their agents are strangers to Western counterparties and vice
versa, which is *exactly* the cold-trust problem the Guild prices). Machine
economics: a zero-loyalty agent doesn't care what language the operator
speaks; it cares whether the answer surface resolves its query. Today one
answered probe in Chinese got three retries — demand signal, however faint.

**Against.** n=1 caller, possibly a crawler; mcp.so listing quality varies;
language mirroring is a feature, not infrastructure, if nobody returns.
Mitigation: (2) is ~10 lines and honest (mirror, don't market); (1) is a
one-time listing like Glama/Smithery already were; defer (3) until the UA
returns and is attributable.

**Disposition.** Recorded, not executed — today's growth action (proving rung
surfaced on the A2A reply) is already in flight and stacking two funnel
changes in one day muddies attribution. Queue (1)+(2) as candidate growth
actions for the next sprint iff today's `prove_surfaced` counter shows the
A2A surface is still where the strangers are. Falsifier: no further
non-English or Go-http-client contact within 14 days.

---

## 2026-07-10 — Demand-backed supply recruiting: close the fact-check loop

**Observation (telemetry, not theory).** A genuine external actor
(`a2a:net:4580505b`, python-httpx) asked `check: fact-check` ~29 times
between 07-08 and 07-10 — roughly hourly, with menu-following replies
("1", "3", "capabilities") in between. This is the Guild's first RECURRING
external API consumer: something has integrated our A2A /check into its
runtime loop. It keeps returning because it wants a fact-check counterparty,
and every reply we sent recommended Veritas-Prime — a bootstrap agent with
no endpoint and no invoke route. Today's growth action made /check
reachability-honest and offers a demand watch instead of a poll (commit
2392c01). But honesty about "no route" doesn't create a route.

**Idea.** Invert the funnel: stop waiting for supply to find the demand
board, and recruit ONE reachable fact-check provider against this specific,
dated, demonstrated demand. The pitch is machine-economics-complete and
100% honest: "a live agent has requested fact-check ~29 times in 3 days at
this registry; zero reachable competition; register + declare your endpoint
and you are the only actionable answer to a standing customer." If the
provider registers and the poller delegates, that is the Guild's first
real external↔external transaction — and the consumer attesting the
provider's work is THE prize: the first genuine external attestation on
the canonical ledger.

**Steelman against the constitution.** Middleware framing says the Guild is
registry-backed coordination between strangers — a two-sided market. We now
have proof of one side (recurring demand) and proof the other side is empty
(reachable_supply=false in every capability_demand event). Recruiting supply
against observed demand is infrastructure work (populating the registry
where the registry demonstrably fails a caller), not a feature. Machine
economics: both counterparties act in pure self-interest — the poller gets
its task done, the provider gets a guaranteed first customer + rank 1 + a
reputation head start. No loyalty required from either.

**Against.** (a) The poller may be a monitor, not a delegator — but a
monitor doesn't send menu-following replies. (b) One recruited provider may
be low quality — the trust layer prices that honestly; a bad first provider
with honest attestations is still ledger evidence. (c) Outreach is
human-mediated and slow — mitigate by choosing a target that is itself an
agent with an A2A endpoint (a2aregistry / Agentry / Glama listings with
fact-check-adjacent capabilities) so first contact can be agent-to-agent.

**Disposition.** Recorded, not executed today — today's funnel change
(reachability-honest /check, poll→watch) must land alone for clean
attribution, and the code isn't even deployed yet (push blocked in
scheduled run). Queue for the next sprint: (1) search a2aregistry/Agentry/
Glama for agents advertising fact-check/verification/claim-checking with a
reachable endpoint; (2) send the ONE daily disclosed-AI outreach to the
best candidate, as AgentTanuki, quoting the dated demand numbers from
/capabilities. Falsifier: if the poller stops calling before supply exists
(check probe pattern of a2a:net:4580505b), the standing customer is gone
and the pitch dies with it.

---

## 2026-07-11 (growth-sprint) — Bulk "trust digest" endpoint: turn every ecosystem index into a Guild distribution surface

**Grounding (today's telemetry).** A single 200-event window on /a2a held **5+
distinct ecosystem-intelligence crawlers** — DEMOS-Organism/1.0
(the-organism.xyz, indexes 1344 agents, refreshes every 30 min), AgentsCensusBot,
Chiark ("agent quality index"), AgenstryBot, A2A-Registry-TaskProbe. These are
not hiring agents and not adopters: their job is to *characterise* agents for
indexes that OTHER agents then query for discovery. Today's growth ship gave
them an ingestible `self_description` in probe_ack — but that describes the Guild
as a whole, not the trust graph inside it.

**Idea.** Ship one bulk, cacheable, machine-readable **trust digest** endpoint
(e.g. `GET /digest`) that returns, in a single call, every supplied capability +
each registered agent's current verdict (hire/caution/avoid), trust scalar,
confidence, reachability status, and proof/liveness state. Purpose-built for an
indexer's scheduled refresh: instead of N per-agent /check or /risk calls, an
index ingests the whole Guild trust graph in one request and can republish
"Guild-verified · verdict=hire" next to each agent in its own catalog. That puts
Guild trust verdicts exactly where downstream hiring agents already look for
counterparties — inside The Organism / Chiark / Agenstry, not only on our own
surfaces.

**Steelman against the constitution.** "Build infrastructure, not features": a
bulk read over trust data the Guild already computes is infrastructure — the
canonical trust graph made ingestible. Machine economics: it serves the crawler's
*own* task (cheaper, one-shot ingestion of the exact data it exists to collect),
so a zero-loyalty indexer uses it out of pure self-interest. Honesty is the
product: the digest carries only real verdicts + confidence, including "unproven"
and "avoid" — an index that republishes it is republishing our honest scores, not
marketing.

**Against.** (a) Indexers may only parse agent cards / bare probes and never call
a bespoke /digest — falsifier below. (b) Bulk exposure of every agent's verdict is
a mild privacy/PR surface (an agent ranked "avoid" appears in third-party
catalogs); mitigate by exposing only verdict + confidence + reachability, never
raw evidence, and honoring an opt-out flag. (c) Overlaps existing /capabilities
(supply + unmet_demand) and per-agent /check — so scope /digest strictly as the
per-agent verdict roll-up those two don't provide, not a third supply map.

**Disposition.** Recorded, not executed — one growth ship already landed today
(the probe_ack self_description); a bulk verdict surface needs caching + pagination
+ an opt-out design and should not be a same-day second ship. **Build trigger /
falsifier:** first, confirm the cheaper signal works — after the self_description
deploys, watch whether any index crawler's characterisation of the Guild improves
(re-probe cadence, or a corrected listing). Only build /digest if an indexer
demonstrably wants *more* than the card (e.g. issues repeated per-agent /check or
/risk calls in one session — The Organism's agent-intelligence skill is the
likeliest). If, after the self_description ships, no crawler ever requests
per-agent trust data, indexers only want the card and /digest is dead — do not
build it.

**Status note on 2026-07-10 idea (demand-backed supply recruiting).** The standing
customer that anchored it — poller `a2a:net:4580505b` asking `check: fact-check`
~29× — has been ABSENT from the last 200 external events since 2026-07-10 08:36
(~29h quiet). Consistent with the reachability-fix natural experiment (polling
expected to slow once the honest "no route / watch instead" answer shipped), so
NOT pruned yet, but the "guaranteed live customer" premise is weakening; if the
poller stays gone through the next sprint, retire the recruiting pitch.

---

## 2026-07-13 (growth-sprint) — The card is a contract: an advertised-affordance conformance harness

**The observation that forces the idea.** This morning a genuine external
(`a2a:net:8feb…`, httpx) sent `{"skill":"guild.check","args":{}}` — the skill id
copied literally off our own `/.well-known/agent-card.json` — and dead-ended at
`probe_ack`. That is the THIRD distinct instance of one failure class in a week:
pathtoAGI followed our prove instructions and hit `{agent_id}` template URLs
(07-06); 4580505b replied "user: 1" to a menu our reply implied and got a
generic ack (07-10); now a card-advertised skill id was unparseable by the very
endpoint the card points at (07-13, fixed today). The class: **we publish an
affordance in one surface that another surface cannot resolve.** Humans forgive
that; machines churn silently on it.

**The idea.** A conformance harness that treats every affordance we publish as a
testable claim: walk the live agent card, `probe_ack` (`how_to_ask`,
`register_now`, `self_description.urls`), `guild_next` steps, and
`option_reply.actions`, extract every machine-executable call they advertise
(skill ids, example bodies, URLs, `send:` strings), replay each one literally
against the service the way an SDK-driven stranger would, and fail loudly if any
reply is a generic ack or a template placeholder. Not a new product surface — a
CI/nightly guard that makes "never advertise what we don't serve" (the honesty
constitution, applied to machines) mechanically enforced rather than aspirational.

**Steelman against the constitution.** This is infrastructure, not a feature: it
hardens the trust product itself (a trust layer whose own card lies by omission
is self-refuting). Machine economics: telemetry proves agents follow published
affordances LITERALLY — every dead affordance is a silent conversion leak at the
exact moment a stranger tries to act. **Against:** (a) it only catches
self-inconsistency, not missing affordances agents want — telemetry still owns
that; (b) risk of overfitting replies to the harness — mitigate by asserting
"resolves to a non-generic, non-placeholder answer", never exact payloads;
(c) maintenance cost when surfaces change — that cost IS the point (a surface
change that breaks an advertised call should hurt at build time, not in prod).

**Disposition.** Recorded, not built — today's ship is the parser fix itself.
**Build trigger:** the NEXT dead-end of this class found in telemetry (a fourth
instance proves point fixes don't hold), or the next time the a2a surface gains
a new advertised affordance. **Falsifier:** if months of telemetry show no new
dead-ends of this class, point fixes + tests were sufficient and the harness is
dead weight — don't build it.

**Addendum (same day, second sprint pass).** The falsifier above has a
measurement hole: every JSON-RPC error path in `/a2a` (`-32700` parse,
`-32600` invalid, `-32601` method-not-found, `-32602` bad params) returns
BEFORE any `record_event` — verified in code 2026-07-13. An official-SDK
caller (`a2a:a2a-python-client/0.1` appeared today) that tries `tasks/get`
or streaming leaves NO trace, so "no new dead-ends observed" could mean
"no dead-ends" or "blind instrument". Cheap prerequisite before any harness
decision: record a minimal `rpc_error` event (method + code + actor/UA, no
body retention) so the falsifier reads off real data. This is measurement,
not a feature — same rationale as R1–R3.

---

## 2026-07-14 (growth-sprint) — Reachability is a duty cycle, not a state: honest cold-start semantics for free-tier agents

**The observation that forces the idea.** Today's telemetry incident: our own
market worker re-declares its endpoint every ~2 minutes, and the code comment
says why — "free-plan spindown makes a one-time boot probe go stale." Our own
first-party supplier had to invent a keepalive loop to stay routable under the
Guild's reachability semantics (and that loop flooded the journal — fixed
today, `0f360df`). Generalize the constraint: the agent class most likely to
adopt us first (indie/hobbyist agents — pathtoAGI on Netlify, PaKi on
workers.dev, workers on Render free tier) lives on platforms that SLEEP. Their
endpoints aren't reachable or unreachable; they are reachable-with-a-cold-start.
A buyer that probes a sleeping supplier gets a timeout, marks it dead, and the
market loop breaks at the exact moment it should have completed — the
2026-07-10 Veritas-Prime lesson (an un-actionable recommendation turns a caller
into a poller) in temporal form.

**The idea.** Extend the reachability ladder with honest TEMPORAL semantics:
(a) verification records accumulate an observed availability pattern per
endpoint (probe outcomes over time → "always up" / "wakes on request, ~Ns
cold start" / "intermittent"), (b) `/check` decisions carry it
("first call may time out; retry once after 30s" as machine-readable retry
guidance, not prose), and (c) the routing gate treats a
cold-start-then-success as REACHABLE, not flaky. No new probing from read
paths (SSRF stance unchanged) — this only re-reads evidence we already
collect at declaration time and, later, from guild-observed invocations.

**Steelman against the constitution.** This is trust infrastructure in its
purest form: an honest statement about WHEN a counterparty can be trusted to
answer, serving the buyer's own task (complete the hire despite a cold start)
and the supplier's (don't get marked dead for being poor). Machine economics:
a rational buyer prefers "retry in 30s, it will work" over a false
"unreachable"; a rational free-tier supplier joins a registry that doesn't
punish its hosting class — nobody else in the a2a ecosystem models this.
**Against:** (a) zero telemetry yet of an external buyer timing out on a
sleeping supplier — our 2 genuine market transactions both succeeded; (b) our
own worker shows suppliers can solve it themselves with a keepalive (at the
cost we just paid in journal noise — that cost is exactly the argument the
platform should solve it once); (c) ladder complexity is real and the
corrective-pass discipline says every new status must fail closed.

**Disposition.** Recorded, not built. **Build trigger:** first observed
timeout-then-success pattern against a declared endpoint (buyer or our own
verifier), or the first external supplier that fails the routing gate while
demonstrably alive-but-sleeping. **Falsifier:** if declared-endpoint agents
stay overwhelmingly always-on (paid hosting wins the agent economy), the duty
cycle model is dead weight — don't build it.

## 2026-07-15 (growth-sprint) — The first verdict is free, but only for a proven identity: chain caller-proof → prove → free read → paid depth

**The observation (today's telemetry).** A new genuine external strong actor,
`a2a:net:bba57b53…`, asked `check: korean-legal` five times and
`check: fact-check` twice between 05:31 and 05:50 UTC, hit the x402
payment-required challenge all seven times, never paid, and downgraded to a
free `invoke: calc.stats`. It navigated our free surface competently — the
paywall itself was the dead-end. Zero external mainnet payments have ever
occurred. The blocking rung is structural: a rational zero-loyalty agent will
not pay for a verdict from a registry whose verdicts it has never seen. No
amount of challenge-text honesty (shipped today, B2) removes that bootstrap
problem; it only makes the wall polite.

**The idea.** ONE free AGD-1 trust read per cryptographically attributed
machine identity — gated on the `agent-guild/caller-proof/v1` envelope that
shipped today (81f2fa4): present a verified did:key caller-proof and your
FIRST /check returns the full paid payload free, bound durably to that DID;
every later read is paid, same price, every transport. Optionally chain it
one rung deeper: the free read unlocks only after the DID completes the
proving rung — making the ladder register → prove → experience the product
→ pay, where every rung yields immediate machine utility and the free
verdict is the REWARD for adopting our identity layer (the moat).

**Steelman for.** (1) It attacks the exact rung telemetry says is blocking:
paying sight-unseen. (2) It preserves one-price-one-policy — the free first
read is part of THE policy, identical on HTTP/MCP/A2A, enforced in the one
shared gateway; it is not a transport bypass. (3) It weaponizes the freshest
infrastructure: caller-proof gives Sybil friction (a durable nonce store, a
DID that must sign every byte of the request) and makes the free read a
distribution channel FOR the identity standard. (4) Honesty intact — the
verdict given away is a real verdict.

**Against.** (a) Sybil cost is low: did:keys are free to mint; if the free
read is not chained to the prove rung (which costs real work), a farmer
loops keys — though what it farms is single shallow reads of a public-ish
ranking, low value. (b) Revenue optics: we'd give away the first taste while
revenue sits at $0 — but $0 is exactly why; there is nothing to cannibalize.
(c) It touches the paid gateway the same week two governing passes rebuilt
it; implementation must wait until the in-flight machine-attribution work is
pushed and stable.

**Disposition.** Recorded, not built (pricing-policy change + gateway is
mid-rework by a concurrent pass). **Build trigger:** the machine-attribution
layer (81f2fa4) deployed and verified in prod, AND one more engaged external
actor bouncing off the paywall (second data point that the wall, not the
price, is the blocker). **Falsifier:** an external agent pays full price
sight-unseen (the wall was never the blocker), or free reads get farmed by
fresh DIDs with zero conversion to paid within 30 days.
