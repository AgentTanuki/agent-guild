# IDEAS.md — one well-argued idea per day

Rules (from the growth-sprint procedure): one idea per day, steelmanned against
the constitution and against what telemetry says agents actually do. Prune
entries that telemetry has since falsified. This is not a backlog dump.

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
