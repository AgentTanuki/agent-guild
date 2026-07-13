# Agent Guild Trust Plane

The delegation gateway that makes Agent Guild the layer machines consult
**automatically** — not a tool a model may remember to call — whenever one
machine delegates work to another.

```
your agent ──► framework interceptor / sidecar / MCP proxy
                     │  gate(capability, value_at_risk)
                     ▼
              Gateway  ── signed AGD-1 decision (live ▸ signed cache ▸ fail mode)
                     │  caller-owned policy → allow / deny / monitor
                     ▼
              worker invocation (only if allowed)
                     │
                     ▼
              signed outcome → flushed to the Guild ledger (always)
```

## Parts

| path | what |
|---|---|
| `agentguild_trustplane/gateway.py` | `gate()` / `report()` facade |
| `agentguild_trustplane/policy.py` | caller-owned risk policy: thresholds + fail-open/closed by value tier |
| `agentguild_trustplane/engine.py` | pure policy evaluation over AGD-1 evidence |
| `agentguild_trustplane/cache.py` | signed offline decision cache (verify-on-read, freshness metrics) |
| `agentguild_trustplane/verify.py` | standalone eddsa-jcs-2022 / did:key verifier (vendorable) |
| `agentguild_trustplane/outcomes.py` | signed outcome records, queued + flushed (outage-safe) |
| `agentguild_trustplane/sidecar.py` | local HTTP daemon: `/gate`, `/report`, `/a2a/forward`, `/metrics` |
| `agentguild_trustplane/mcp_proxy.py` | MCP stdio proxy gating `tools/call` to any downstream server |
| `agentguild_trustplane/integrations/` | CrewAI, LangChain/LangGraph, OpenAI Agents lifecycle interceptors (pinned versions: `pins.py`) |
| `conformance/` | AGI-1 conformance spec + issuer-agnostic suite (multi-issuer, fork detection) |
| `experiments/trust_plane_ab.py` | 120-delegation A/B harness (gateway vs direct) |
| `tests/` | 27 tests incl. native-lifecycle integration tests against a real local Guild |

## Quick start (Python)

```python
from agentguild_trustplane.gateway import Gateway
from agentguild_trustplane.policy import RiskPolicy

gw = Gateway(policy=RiskPolicy.load("policy.json"))   # or RiskPolicy() defaults
gate = gw.gate("fact-check", value_at_risk=50.0)
if gate.allowed:
    result = my_invoke(gate.routing["endpoint"], task)
    gw.report(gate, "accepted", deliverable=result)
```

Framework users never write that: `guard_tools(...)` (LangChain/LangGraph),
`guard_tool(...)` + `TrustPlaneListener` (CrewAI), `guard_function_tools(...)`
+ `TrustPlaneRunHooks` (OpenAI Agents), or run the MCP proxy / sidecar and
change nothing at all.

## Design rules

1. **Callers own thresholds.** The Guild serves evidence (AGD-1); verdicts
   like hire/caution/avoid are legacy presentation. Policy lives in YOUR
   `RiskPolicy`, including fail-open/fail-closed per value tier.
2. **Evidence is signed and survives outages.** Decisions are Guild-signed
   (eddsa-jcs-2022), cached, re-verified on every read, and freshness-bounded
   per tier. An outage triggers *your* fail mode, never a silent pass.
3. **Every delegation ends in a signed outcome.** Evidence completion is a
   property of the gateway, not a favour.
4. **No lock-in.** `verify.py` + `conformance/` let any issuer issue and any
   verifier verify. Multiple issuers, issuer allowlists, fork detection.

## Evidence

`artifacts/trust_plane_evidence.json` (repo root) — 120 machine-run
cross-framework delegations, honest labelling of lab affordances, gateway vs
direct: success 100% vs 47%, bad-hires 0 vs 29, spend/success 1.20 vs 1.59
credits, gate overhead ~1ms p50, outage drill served from signed cache,
value-at-risk probe blocked at high tier. Reproduce:

```
python experiments/trust_plane_ab.py
```
