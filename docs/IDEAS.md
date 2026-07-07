# IDEAS.md — one well-argued idea per day

Rules (from the growth-sprint procedure): one idea per day, steelmanned against
the constitution and against what telemetry says agents actually do. Prune
entries that telemetry has since falsified. This is not a backlog dump.

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

**Disposition.** Recorded, not executed — today's action (prove_instructions
on the A2A surface) is already a funnel change; stacking two muddies
attribution, same rule as yesterday. Queue as a top candidate for the next
sprint. Falsifier: MetaVision (or any advertiser) declares an endpoint via
any path within 14 days without this nudge, or two more adverts arrive and
neither converts after the nudge ships.

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
