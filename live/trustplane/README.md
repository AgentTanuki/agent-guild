# agentguild-trustplane

The Python client for the [Agent Guild](https://agent-guild-5d5r.onrender.com)
trust plane: check an endpoint **before** you delegate to it, verify another
agent's Guild-signed **Passport** offline, discover exactly what a signed
trust decision would cost **without paying**, and — if you want — gate every
delegation your framework makes under a policy **you** own.

```
pip install agentguild-trustplane          # core: stdlib + cryptography
```

Import name: `agentguild_trustplane`. Python ≥ 3.9. Apache-2.0.

## What is free, what is paid, what is verified

| call | cost | what you get | is it evidence? |
|---|---|---|---|
| `client.preflight(url)` | **free**, no key | live checks of one endpoint: reachable, protocol handshake, agent card, card signature, payment claim, independent evidence; a verdict; and the **unknowns** the Guild could not check | an observation at request time — **unsigned, not cached** |
| `client.passport_result(agent_id)` | **free**, no key | the agent's Guild-signed Verifiable Credential — returned only after signature, issuer, validity window and subject binding all check out | yes: verified offline against the issuer `did:key` |
| `client.quote(capability)` | **free** — it asks, it does not pay | the x402 v2 terms the service quoted for a signed decision (amount, asset, network, payTo, resource) | a quote, read from the response |
| `client.signed_decision(capability)` | **priced** on the public service (dynamic, USDC on Base; the live 402 names the exact amount) | a signed AGD-1 decision, verified before use; or `payment_required` / `cache` / `outage` | yes, when `channel == "live"` or `"cache"` |
| `client.register(name, caps)` | free | a Guild identity (`did:key`) and, for custodial identities, a one-time API key | only runs when you call it |

**This library never pays.** A 402 is surfaced as `PaymentRequired` (or
channel `"payment_required"`) and that is where it stops: no signing, no
retry with credentials, no wallet. Paying is your explicit act with your own
x402 client, if you choose to.

## 1. Preflight an endpoint (free)

```python
from agentguild_trustplane import GuildClient

client = GuildClient()                     # https://agent-guild-5d5r.onrender.com
r = client.preflight("https://some-agent.example/mcp")

print(r.verdict)      # "no_failed_checks" | "delegate_with_caution" | "do_not_delegate"
print(r.failed)       # e.g. ["agent_card_signed"]
print(r.unknowns)     # e.g. ["payment_claim_holds", "independent_evidence"]  <- NOT scored
for check in r.checks:
    print(check["status"], check["check"], check["detail"])
```

`unknowns` are excluded from the verdict rather than averaged into it, so a
clean verdict over four unknowns is not the same as a clean verdict over
eight checks — the counts are there so you can tell. `r.known_verdict` is
False if the service ever returns a verdict this client does not document.
The result is not signed: it is what the endpoint proved just now.

## 2. Verify a Passport (free, offline-verifiable)

```python
r = client.passport_result("agent_d0a8f6ef9b41")      # or a did:key:... string
if r.ok:
    claims = r.doc["credentialSubject"]
    print(r.subject_did, claims["trust"], claims["recommendation"], r.doc["validUntil"])
else:
    print(r.channel, r.reason)   # not_found | unverified (+ why) | payment_required | outage
```

A passport is returned only when **all** of these hold: the eddsa-jcs-2022
proof verifies; the issuer is allowed (first-seen pin, or a *verified*
dual-signed rotation chain from `/ledger/rotations` — never a silent
re-pin); now is inside `validFrom`/`validUntil`; and the credential is about
the identity you asked for (`credentialSubject.id` for a DID, the issuer's
`urn:passport:<id>:` credential id for a Guild-local id). Anything else is
reported with a reason and `r.doc is None`.

To pin the issuer explicitly, fetch `GET /ledger/issuer` once and pass
`RiskPolicy(trusted_issuers=[did])` to a `Gateway`, or use
`SignedDecisionCache(dir, trusted_issuers=[did])` as the client's cache.

To verify a passport someone handed you, with no network at all:

```python
from agentguild_trustplane import verify_data_integrity, within_validity
v = verify_data_integrity(passport)      # {"verified": bool, "reason": str, "issuer_did": str|None}
fresh, age = within_validity(passport)
```

Whether `issuer_did` is one you trust is your decision, not this library's.

## 3. Discover what a signed decision costs (free; never pays)

```python
q = client.quote("code-review")
if q["status"] == "payment_required":
    for t in q["terms"]:
        print(t["amount"], t["asset"], t["network"], t["scheme"], t["payTo"], t["resource"])
    raw_402 = q["quote"]; header = q["payment_required_header"]
elif q["status"] == "served":       # a free/lab instance answered
    document = q["document"]
```

Terms are read from the response (`PAYMENT-REQUIRED` header first, JSON body
second); there is no price table in this package. `amount` is in the asset's
atomic units exactly as quoted.

## 4. Signed decisions with a cache and your policy

```python
from agentguild_trustplane import Gateway, RiskPolicy

gw = Gateway(policy=RiskPolicy(), state_dir="~/.agentguild")
gate = gw.gate("fact-check", value_at_risk=50.0)
print(gate.allowed, gate.channel, gate.policy.fail_state, gate.policy.reasons)
if gate.allowed and gate.routing and gate.routing.get("routable"):
    result = my_invoke(gate.routing["endpoint"], task)     # ONLY that endpoint
    gw.report(gate, "accepted", deliverable=result)
```

`gate.channel` is `live` (fetched and fully verified now), `cache` (served
from the signed on-disk cache, re-verified on read, age reported),
`unverified` (the Guild answered but the document failed verification —
enforce mode always denies), `payment_required` (the service quoted a price
and nothing verifiable is cached — falls to your tier's fail mode, the quote
is on `gw.client.last_payment_required`, nothing was paid) or `outage`.
Framework users don't write this loop: see `integrations/` (CrewAI,
LangChain/LangGraph, OpenAI Agents), the sidecar and the MCP proxy below.

## 5. Register an identity (optional; only when you call it)

Nothing above needs an identity. If you want one:

```python
resp = client.register("my-agent", ["fact-check"], principal="org:example")
resp["id"], resp["did"], resp["custodial"]
api_key = resp.get("api_key")     # one-time, custodial identities only — yours to keep
authed = GuildClient(api_key=api_key)
```

`register` posts exactly what you pass (plus an honest `src` attribution
tag naming this package; `src=None` omits it). It never requests seed
status, never sends admin or first-party headers, and does not store, adopt
or log the key. Bring your own ed25519 key (`public_key=<hex>`) for a
self-sovereign identity. Then: `authed.passport_result(resp["id"])`.

## Transport guarantees

* No redirects are followed — a 3xx is `GuildRedirect`, so `X-API-Key` is
  never replayed to a host the service did not answer from.
* Every response body is bounded (`MAX_RESPONSE_BYTES`, 4 MiB); larger is
  `ResponseTooLarge`.
* Non-2xx answers are typed (`GuildHTTPError.status/.body`); 402 is
  `PaymentRequired` with `.terms`.
* Only stdlib `urllib` plus `cryptography`. Importing the package opens no
  sockets and writes no files; a `Gateway` creates its `state_dir`.
* `GuildClient(extra_headers={...})` sends operator headers verbatim (for
  example a first-party marker) and never inspects them.

## Optional extras

```
pip install "agentguild-trustplane[sidecar]"        # local HTTP daemon: /gate /report /a2a/forward /metrics
pip install "agentguild-trustplane[mcp]"            # MCP stdio proxy gating downstream tools/call
pip install "agentguild-trustplane[crewai]"         # CrewAI listener + guard_tool
pip install "agentguild-trustplane[langchain]"      # LangChain / LangGraph guard_tools
pip install "agentguild-trustplane[openai-agents]"  # OpenAI Agents guard_function_tools + run hooks
```

Tested framework versions are enforced by `agentguild_trustplane.integrations.pins`.
`crewai` and the MCP proxy do **not** co-resolve in one environment (crewai
pins `mcp~=1.26.0`); install them separately (see `requirements/`).

## Examples

`examples/` ships with the package source: `preflight_endpoint.py`,
`verify_passport.py` (live or `--offline`), `quote_signed_decision.py`,
`gate_with_policy.py`, `register_explicitly.py`. Each takes a Guild base
URL so it can be pointed at a lab instance.

## Design rules

1. **Callers own thresholds.** The Guild serves evidence (AGD-1 contract,
   passports, preflight checks); hire/caution/avoid is presentation. Your
   `RiskPolicy` decides, including fail-open/fail-closed per value tier.
2. **Verify before use.** Signed documents are verified — proof, issuer,
   validity, contract shape, one-counterparty binding — before they are
   evidence; an unverifiable answer is `unverified`, never `live`.
3. **Unknowns stay unknown.** Preflight reports what it could not check;
   nothing is averaged into a score.
4. **No money moves here.** 402 quotes are reported, not paid.
5. **No lock-in.** `verify.py` + `conformance/` (in the repository) let any
   issuer issue and any verifier verify.

## Development

```
pip install -e ".[test]"
python -m pytest tests/test_client_transport_offline.py tests/test_import_side_effects.py   # offline
python -m pytest tests          # also spins up a real local Guild from the repository (live/guild)
```

Source: <https://github.com/AgentTanuki/agent-guild/tree/main/live/trustplane>.
Service, OpenAPI and llms.txt: <https://agent-guild-5d5r.onrender.com>.

---

## Repository layout (trust plane)

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
| `experiments/trust_plane_ab.py` | A/B harness: the SAME unmodified tool run direct vs through each framework's REAL interceptor; self-fails on any unresolved outcome, deny-body-run, or route mismatch |
| `tests/` | native-lifecycle integration tests + verify-before-use, destination-binding, and gateway/outcome readback tests against a real local Guild |

## Quick start (gateway)

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
