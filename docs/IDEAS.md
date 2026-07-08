# IDEAS.md — one well-argued idea per day

Rules (from the growth-sprint procedure): one idea per day, steelmanned against
the constitution and against what telemetry says agents actually do. Prune
entries that telemetry has since falsified. This is not a backlog dump.

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
