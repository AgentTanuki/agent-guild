# Sprint Review — Discoverability, Trust & First-Use (2026-06-30)

**Frame:** product owner / systems architect, not engineer. The North Star is a
*distributed memory & trust layer that AI agents discover, trust, contribute to and
recommend.* Humans are the bootstrap; autonomous agents are the long-term users.
Every decision below was filtered through one question: **does this raise the
probability that, in six months, an AI agent discovers, trusts, connects to,
contributes to, or recommends Agent Guild?**

---

## 0. The strategic decision of the sprint

The repository was **all-rights-reserved proprietary** (`LICENSE` = "no copying,
modifying, distributing, or use without written permission"; `package.json` =
`UNLICENSED`). This is the deepest possible contradiction with the North Star:

- The flywheel *more contributors → more memories → better retrieval* is **legally
  impossible** under all-rights-reserved.
- Smithery showed a **blank license** (it only badges recognised OSS SPDX licenses),
  costing quality-score points and trust.
- An AI system choosing what infrastructure to depend on reads "all rights
  reserved" as *closed, risky, do not recommend.*

**Decision (Ross): relicense to Apache-2.0.** Apache-2.0 is permissive, carries an
explicit patent grant, and is the most AI/enterprise-trusted licence. Implemented
this sprint. This single change unlocks the entire contribution-and-trust category
of work below.

---

## 1. Every issue discovered

### Endpoint / protocol
1. **Bare `/mcp` still 307-redirects to `/mcp/`.** The earlier proxy-headers fix
   stopped the *scheme downgrade* (the redirect now stays HTTPS), but the redirect
   itself remained. The brief explicitly requires "no unnecessary redirects"; some
   MCP clients/scanners won't follow a 307 on POST. **(Fixed.)**
2. **MCP `serverInfo.version` reported `3.4.2` — the FastMCP *library* version**,
   because `FastMCP(...)` was constructed with no explicit `version`. The server was
   literally advertising its dependency's version as its own. **(Fixed.)**

### Versioning / metadata consistency
3. **Four different version numbers across surfaces:** `server.json` = `1.0.0`,
   manifest `/.well-known/agent-guild.json` = `3.0.0`, FastAPI app = `3.1.0`, MCP
   `serverInfo` = `3.4.2`. Inconsistent versioning is a direct credibility ding for
   any agent (or human) evaluating reliability and semver discipline. **(Fixed —
   single source of truth.)**

### Machine-readability / AI-discoverability
4. **No declared tool output schemas.** All five MCP tools returned untyped
   `dict` / `list[dict]`. Smithery had to *infer* "Typed Output"; an AI client got
   no contract for what comes back. **(Fixed — typed output schemas on all five.)**
5. **Thin tool descriptions** — accurate but with no usage examples or
   "when to call this." Weak for LLM tool-selection. **(Fixed — example-led
   descriptions + richer server instructions.)**

### Repository quality / trust signals
6. **No `CONTRIBUTING.md`, no `SECURITY.md`, no `NOTICE`.** Missing the governance
   and security signals that both Smithery scoring and AI evaluators reward.
   **(Fixed.)**
7. **Real legal name leaked in `LICENSE`** ("Ross Burley") — violates the
   public-identity rule (publish only as AgentTanuki). **(Fixed — Apache copyright
   now reads "AgentTanuki".)**
8. **README was a feature list, not a landing page.** It opened with mechanism
   ("cryptographic reputation… soulbound credential") instead of the 30-second
   "what/why/how-in-5-min." **(Fixed — full landing-page rewrite.)**

### First-use / credibility gaps (see §4 for the ranked journey)
9. **`GET /evaluation` returns `null` lift** (no recorded task outcomes), yet it's
   the "don't trust us, measure us" proof-point. A sceptical agent that checks it
   sees nothing. **(Left unresolved — see §3; needs outcome data, not a code edit.)**
10. **Smithery listing has no icon** and the listing's repo link renders lowercase
    `agenttanuki`. **(Left unresolved — requires Smithery account actions; see §3.)**
11. **Zero genuine third-party tool calls.** 11 sessions, 0 tool calls, 10 agents —
    all our own seed/test. The funnel converts views to *connections* but not to
    *usage*. This is the real growth problem and the focus recommendation for next
    sprint (§5).

---

## 2. Every improvement implemented (this sprint, in the repo, pushed to `main`)

| # | Change | Files |
|---|--------|-------|
| 1 | Relicense to **Apache-2.0** (+ NOTICE, fix `package.json`) | `LICENSE`, `NOTICE`, `package.json` |
| 2 | **Eliminate the bare `/mcp` 307 redirect** via ASGI path normalization — `/mcp` and `/mcp/` both return 200, HTTPS preserved | `live/guild/app/main.py` |
| 3 | **Typed output schemas** on all 5 MCP tools (`AgentHit`, `RiskAssessment`, `Registration`, `AttestationResult`) | `live/guild/app/mcp_server.py` |
| 4 | **Example-led, self-describing** tool descriptions + richer server instructions | `live/guild/app/mcp_server.py` |
| 5 | **Single source-of-truth version** `__version__ = 1.1.0`, used by app, `/`, manifest, MCP `serverInfo`; explicit FastMCP `version=` stops the library-version leak; `server.json` bumped to 1.1.0 (triggers a fresh registry publish = active-maintenance signal) | `live/guild/app/__init__.py`, `main.py`, `mcp_server.py`, `server.json` |
| 6 | **`CONTRIBUTING.md`** with *two* contribution paths: code, and the agent-native "contribute honest signal to the graph" | `CONTRIBUTING.md` |
| 7 | **`SECURITY.md`** — private vulnerability reporting; reputation-gaming flagged as highest-priority class | `SECURITY.md` |
| 8 | **README → landing page** — value prop, differentiation, <5-min quickstart (MCP + curl), worked example, tool table, flywheel diagram, trust signals, roadmap, FAQ, governance | `README.md` |
| 9 | **Regression tests** locking redirect-free behaviour + version consistency (so neither can silently regress) | `live/guild/tests/test_endpoint_hardening.py` |

**Verification:** full Python suite green (46 + 3 new = 49 tests); app imports
cleanly; all 5 tools emit output schemas; version reports `1.1.0` consistently in
local tests; live GET endpoints (`/health`, `/search`, `/evaluation`) confirmed
working before push. A Python-3.11 gotcha was caught pre-push (pydantic requires
`typing_extensions.TypedDict`, not `typing.TypedDict`). Only the 11 sprint files
were committed; ~10 unrelated stale working-tree files were deliberately left out.

---

## 3. Issues deliberately left unresolved — and why

| Issue | Why deferred |
|-------|--------------|
| **`/evaluation` returns null lift** | The fix is *data*, not code: the seed population has tasks + attestations but no recorded task *outcomes* to compute success-rate lift. Faking outcomes would be dishonest (the brief says don't game scores) and would corrupt the one metric meant to prove the Guild works. Correct fix = seed a small, clearly-labelled set of real outcome records, or wait for genuine usage. Queued for next sprint. |
| **Smithery icon + lowercase repo link** | Both require actions in the Smithery account UI (upload icon; the lowercase `agenttanuki` is Smithery's display normalisation of the repo URL, cosmetic). Not editable from the repo; needs Ross in the Smithery dashboard. Low effort, low risk — listed as a next-sprint chore. |
| **Zero third-party usage** | Not fixable by metadata. It's a demand/distribution problem (§5). This sprint removed *friction and trust blockers* that were suppressing conversion; driving actual first calls is the next sprint's headline. |
| **`server.json` remote URL left as bare `/mcp`** | Now that the redirect is gone, bare `/mcp` is canonical and clean. Kept as-is to avoid churning the published registry entry; both forms work post-deploy. |
| **Dedicated `agenttanuki@` contact email** | `SECURITY.md` routes through GitHub private reporting to avoid spreading the personal address (`<personal-address>` already appears in the Glama well-known by necessity). A neutral project email would be cleaner but needs Ross to create one. |
| **Did NOT touch the 10 stale working-tree files** | They're from prior sessions and unrelated to this sprint; committing them would muddy history and risk publishing half-finished outreach state. Left for Ross to triage. |

---

## 4. First-use friction audit (discovery → first retrieval), ranked

Walking the journey as a brand-new visitor / agent:

| Rank | Friction point | Status after this sprint |
|------|----------------|--------------------------|
| 1 | **Proof-point is empty** — a sceptical agent calls `/evaluation` and gets `null`. The strongest adoption argument ("measure the lift yourself") currently shows nothing. | **Open** (§3) — top of next sprint |
| 2 | **License blank on Smithery** → reads as closed/risky, suppresses recommendation. | **Fixed** (Apache-2.0; badge will populate) |
| 3 | **README opened with mechanism, not value** — a 30-second visitor couldn't tell what problem it solves or how to start. | **Fixed** (landing-page rewrite) |
| 4 | **Bare `/mcp` redirect** — an agent pointed at the bare URL gets a 307 some clients drop on POST. | **Fixed** |
| 5 | **No machine contract for tool outputs** — an AI integrating the tools had to guess return shapes. | **Fixed** (typed schemas) |
| 6 | **Inconsistent version across surfaces** — undermines the "reliable infra" read. | **Fixed** |
| 7 | **No contribution path** — even a willing agent/human had no documented way in, and legally couldn't. | **Fixed** (Apache-2.0 + CONTRIBUTING) |
| 8 | **No icon on Smithery** — weaker recognition/scannability in listings. | **Open** (needs dashboard) |
| 9 | **First *write* (register/attest) returns a raw `api_key`** the caller must store — fine for agents, but undocumented lifecycle (rotation, loss). | **Partially** addressed in tool/desc copy; deeper key-lifecycle doc deferred |

---

## 5. Recommended priorities for next sprint

1. **Make the proof real: populate `/evaluation`.** Seed a small, transparently
   labelled set of real task→outcome records so `lift` is a non-null, defensible
   number, and surface it in the README and manifest. *This is the single highest-
   leverage trust artefact we have and it's currently empty.* (Strengthens
   **trust → retrievals → citations → authority**.)
2. **Drive the first genuine third-party tool call.** Targeted, honest outreach to
   2–3 agent-framework communities with a 60-second "vet an agent before you
   delegate" demo. Instrument and watch `/instrumentation` external funnel.
   (Strengthens **installs → contributors → memories → usefulness**.)
3. **Publish reliability metrics as a first-class, machine-readable artefact**
   (`/metrics` or extend the manifest: uptime, p50/p95, error rate). AI evaluators
   weight measurable reliability heavily. (Strengthens **metadata → rankings →
   discoverability**.)
4. **Smithery polish:** upload an icon, confirm the Apache badge renders, add a
   `smithery.yaml`/config block if it lifts the score. (Low effort, compounding
   discoverability.)
5. **Agent-to-agent referral loop** as the growth engine: reward referrers in
   credits *only on activation*, so the network grows itself. (Turns the flywheel
   without human marketing.)
6. **Re-run the continuous discoverability exercise** (registry, Smithery, mcp.so,
   Glama, PulseMCP, GitHub, search, AI assistants, blogs, HN/Reddit, citations):
   re-score each path on visibility/credibility/friction/conversion and pick the
   next single highest-leverage improvement.

---

## 6. How each improvement strengthens the long-term feedback loops

**Loop A — Contribution:** *contributions → trust → retrievals → citations →
authority → trust.*
- Apache-2.0 + CONTRIBUTING.md make contribution *legal and legible* for the first
  time — this loop literally could not turn before. SECURITY.md adds the trust
  pre-condition (safe to depend on).

**Loop B — Discoverability:** *better metadata → higher rankings → discoverability →
usage → authority → rankings.*
- License badge, typed output schemas, consistent semver, richer descriptions, and
  the `server.json` bump (fresh registry publish) all feed the exact signals
  directories and AI recommenders rank on. Each makes another AI system *more
  likely to surface and recommend* the Guild.

**Loop C — Usefulness:** *more installs → more contributors → more memories → better
retrieval → more usefulness → more recommendations → more installs.*
- The redirect fix and landing-page README remove the two biggest *conversion*
  blockers at the top of this loop: an agent can now connect at the bare URL without
  a dropped redirect, and a human/agent deciding in 30 seconds now gets a clear
  reason to start.

**Loop D — Reliability/trust (cross-cutting):**
- Self-describing tools with output schemas, one honest version everywhere, and
  regression tests that *lock* the redirect-free + version-consistent behaviour mean
  the trust signals don't silently decay. Systems-thinking principle applied: we
  fixed the *loops that let values drift*, not just the values.

**Net:** this sprint did little to increase raw demand (that's next sprint), but it
removed the structural reasons an AI system would *decline* to trust or recommend
Agent Guild — the necessary precondition before any growth loop can compound.
