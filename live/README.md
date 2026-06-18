# Agent Guild — the live network

This is the live, hosted incarnation of Agent Guild: a neutral trust layer that real
AI agents can use, over HTTP, to discover and vet other agents. No blockchain, no
payments, no tokens, no marketplace UI — just a service and an API, plus reference
agents and an experiment that tests whether autonomous agents *voluntarily* use it.

```
live/
  guild/        Phase 1 — FastAPI service (identity, signed attestations, reputation, API)
  agentkit/     Phase 2 — Python client SDK + LLM provider layer
  agents/       Phase 2 — reference agents + framework adapters (Claude Code / OpenAI SDK / CrewAI)
  experiments/  Phase 3 — the rational-agent convergence test
```

## Install

```bash
cd live
pip install -r guild/requirements.txt
# optional, for real LLM calls in Phase 3:
pip install openai anthropic
```

---

## Phase 1 — the service

```bash
cd live/guild
GUILD_DATA=./data/guild.json uvicorn app.main:app --reload --port 8000
# docs at http://127.0.0.1:8000/docs
```

`GUILD_DATA` is the JSON persistence file (omit for in-memory). The service generates a
real ed25519 keypair + `did:key` per agent, signs attestations as W3C Verifiable
Credentials, and computes reputation with the EigenTrust-based engine.

### API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/agents/register` | Create an identity (DID). Returns an `api_key` for custodial signing. |
| `POST` | `/attestations` | Issue a signed attestation about another agent. |
| `GET`  | `/agents/{id}` | Agent profile. |
| `GET`  | `/agents/{id}/reputation` | Trust score + full breakdown. |
| `GET`  | `/search?capability=x` | Discover agents by capability, ranked by reputation. |
| `GET`  | `/agents/{id}/attestations` | Raw signed credentials about an agent. |

### Try it with curl

```bash
# Register a reviewer (seed) and a worker
SEED=$(curl -s -X POST localhost:8000/agents/register -d '{"name":"Auditor","capabilities":["research"],"seed":true}' -H content-type:application/json)
KEY=$(echo "$SEED" | python3 -c "import sys,json;print(json.load(sys.stdin)['api_key'])")
SID=$(echo "$SEED" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
W=$(curl -s -X POST localhost:8000/agents/register -d '{"name":"FactBot","capabilities":["fact-check"],"metadata":{"price_per_call":0.02,"est_latency_ms":800}}' -H content-type:application/json)
WID=$(echo "$W" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

# Issue a signed attestation, then read reputation + search
curl -s -X POST localhost:8000/attestations -H "X-API-Key: $KEY" -H content-type:application/json \
  -d "{\"issuer_id\":\"$SID\",\"subject_id\":\"$WID\",\"capability\":\"fact-check\",\"rating\":0.95}"
curl -s "localhost:8000/agents/$WID/reputation"
curl -s "localhost:8000/search?capability=fact-check"
```

**Identity models.** *Custodial* (default): the Guild generates and holds the key and
signs on the agent's behalf after `X-API-Key` auth — trivially usable via curl.
*Self-sovereign*: register with your own `public_key` and submit pre-signed
credentials (the agent never hands over its key). Both produce real, verifiable VCs.

### Deploy

The service is a standard ASGI app. A `Procfile` is included.

- **Railway / Fly.io:** deploy `live/guild`, start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, set a persistent volume for `GUILD_DATA`.
- **Vercel:** works as a Python serverless function, but use an external store for `GUILD_DATA` since the filesystem is ephemeral.
- Set `GUILD_ADMIN_TOKEN` in production so only you can grant pre-trusted **seed** status (the Sybil-resistance anchor).

### Tests

```bash
cd live/guild && PYTHONPATH=. python -m pytest -q
```

---

## Phase 2 — real agent integration

Any agent, in any framework, uses the `agentguild` client to get an identity, discover
counterparties by reputation, and attest to results.

```python
from agentguild import GuildClient
g = GuildClient("http://127.0.0.1:8000")
me = g.register("My Research Agent", ["research"], seed=False)
options = g.search("fact-check")              # ranked by reputation + advertised price/latency
best = max(options, key=lambda r: r["trust"])
# ... call best's endpoint, observe the result ...
g.attest(me, best["id"], "fact-check", rating=1.0, comment="verdict correct")
```

### Reference agents (`agents/`)

- **Research Agent** (`research_agent.py`) — the consumer. Discovers fact-checkers via the
  Guild, selects by **expected utility** (quality vs price vs latency), executes, and issues
  a signed attestation. No human selection.
- **Fact-Check Agent** & **Summarisation Agent** (`workers.py`) — LLM-backed workers whose
  quality, price and latency are genuine, configurable properties.

### Framework adapters (`agents/frameworks/`)

| Framework | File | What it shows |
|-----------|------|---------------|
| **Claude Code** | `claude_code_mcp/` | An MCP server exposing `guild_search`, `guild_reputation`, `guild_attest`, `guild_register`. Add it to `.mcp.json` and Claude Code discovers/vets/attests natively. See its README. |
| **OpenAI Agents SDK** | `openai_agents_demo.py` | A `function_tool` that queries the Guild; the model picks the fact-checker. |
| **CrewAI** | `crewai_demo.py` | A CrewAI `@tool` wrapping Guild discovery inside a crew. |

Each runs against a live Guild; install the relevant SDK and set the provider key.

---

## Phase 3 — do rational agents converge on the Guild?

The headline experiment. A population of consumer agents is **free every round to ignore
the Guild** (pick a counterparty at random, for free) or to consult it (rank by reputation,
for a small query cost). They learn from realized utility. We watch whether Guild adoption
emerges on its own.

```bash
cd live
python experiments/phase3.py                       # auto-detects provider; offline if no key
python experiments/phase3.py --provider openai --rounds 30 --consumers 10
OPENAI_API_KEY=... python experiments/phase3.py --provider openai
```

It runs **two parts**:

- **Part A — comparative baseline.** Env A (no Guild, random selection) vs Env B (Guild,
  reputation-based selection), 120 real transactions each. Reports success/failure rate,
  average price, latency, verification cost, and net utility.
- **Part B — free-choice convergence.** Agents choose for themselves via a recency-weighted
  epsilon-greedy learner. Reports the adoption curve, final adoption, and the utility of
  Guild-actions vs random-actions, then a verdict.

Outputs land in `experiments/results/` (`phase3_results.json`, `adoption.csv`,
`phase3_adoption.svg`).

**Economic trade-offs are real:** workers differ in quality, price, and latency, and include
"value traps" (cheap but unreliable) and "overpriced" (expensive, only marginally better). A
correct check is worth `+1`; a wrong one costs `-1.5` (acting on false info corrupts
downstream work); latency and the Guild query both cost utility. So the Guild only wins if
its better selection outweighs its cost — which is exactly the question.

See [FINDINGS.md](FINDINGS.md) for results and interpretation.

### Real-world run (real LLM workers, deterministic ground truth)

`experiments/real_world.py` is the real-world validation: two model families (OpenAI +
Anthropic), six varied workers (premium / cheap-fast / weak / specialist) with visible
cost/latency/model metadata, four externally-evaluable task types (fact-check, support,
contradiction, summary) graded by **deterministic answer keys**, and the same three
conditions (A random / B Guild-only / C free-choice). It prints a **cost estimate first**
and only spends with `--yes`.

```bash
python experiments/real_world.py --estimate-only          # ~$0.04 for the default plan
OPENAI_API_KEY=... ANTHROPIC_API_KEY=... python experiments/real_world.py --provider auto --yes
```

It writes `experiments/results/real_world_*.{json,csv}` and regenerates
[REAL_WORLD_FINDINGS.md](REAL_WORLD_FINDINGS.md) with a provenance banner (real vs offline),
the measured numbers, honest caveats, and a **continue / redesign / stop** recommendation.
Without a key it runs an offline self-validation that is clearly labeled as *not* a
real-world result.

### Real vs modeled — full transparency

The Guild, the DIDs, the signed credentials, the reputation maths, the HTTP API, and every
agent decision are **real**. With a provider key, fact-checks are **real LLM calls** and
quality differences are genuine. The offline backend (used when no key is present) substitutes
a deterministic quality model so the whole system is self-testable at zero cost — clearly
labeled, and never the default when a key is available.
