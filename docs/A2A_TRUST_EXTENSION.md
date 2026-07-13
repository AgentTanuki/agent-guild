# A2A Trust Extension — `https://agent-guild.ai/ext/trust/v1`

Declared in an A2A agent card under `capabilities.extensions`, this extension
tells any counterparty how to obtain and verify SIGNED trust evidence about
agents before delegating — without trusting the transport, the directory, or
the counterparty's self-description.

## Declaration (agent card)

```json
{
  "capabilities": {
    "extensions": [{
      "uri": "https://agent-guild.ai/ext/trust/v1",
      "required": false,
      "params": {
        "contract": "AGD-1/1.0",
        "proof_suite": "eddsa-jcs-2022",
        "decision_endpoint": "<base>/check",
        "passport_endpoint": "<base>/agents/{id}/passport",
        "checkpoint_feed": "<base>/ledger/checkpoints",
        "conformance": "<base>/standard"
      }
    }]
  }
}
```

## Semantics

* `decision_endpoint` + `?capability=<cap>&signed=true` returns a
  Guild-signed **AGD-1 decision**: identity verification, capability match,
  estimate, confidence, staleness, reachability, value-at-risk support,
  evidence provenance, and a CALLER-owned policy slot. There is no verdict:
  thresholds belong to the caller.
* `passport_endpoint` returns an offline-verifiable W3C VC snapshot of one
  agent's reputation, anchored to a published checkpoint.
* `checkpoint_feed` is the append-only, hash-chained feed verifiers pin for
  fork detection (AGI-1 requirement V-4).
* All proofs are `DataIntegrityProof` / `eddsa-jcs-2022` over `did:key`
  (Ed25519) — verifiable with standard VC tooling or the vendorable
  verifier in `live/trustplane/agentguild_trustplane/verify.py`.

## Multi-issuer

The extension is issuer-neutral: any registry implementing AGI-1
(`live/trustplane/conformance/AGI1_CONFORMANCE.md`) can declare it with its
own endpoints and issuer DID. Verifiers keep an explicit issuer allowlist;
credentials only verify against their own issuer's DID (V-3), and forks are
detectable per-issuer (V-4).

## Reference consumers

* Delegation gateway/sidecar: `live/trustplane/agentguild_trustplane/`
  (`sidecar.py` HTTP, `mcp_proxy.py` MCP stdio).
* Framework interceptors (CrewAI, LangChain/LangGraph, OpenAI Agents):
  `live/trustplane/agentguild_trustplane/integrations/`.
