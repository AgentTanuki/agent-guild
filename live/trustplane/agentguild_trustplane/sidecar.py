"""Local HTTP sidecar: the gateway for processes that aren't Python.

Run:  python -m agentguild_trustplane.sidecar --port 8787 --policy policy.json

Endpoints (localhost only by default):
  POST /gate            {capability, value_at_risk?, context?} -> GateResult
  POST /report          {gate_id, outcome, deliverable?, latency_ms?, cost?}
                        The gate_id MUST identify a gate issued by THIS
                        sidecar; unknown gate ids are rejected (404) and the
                        caller can never change the worker/provider identity
                        an outcome is credited to.
  POST /a2a/forward     {capability, value_at_risk?, endpoint?, payload}
                        gate -> forward JSON-RPC to the ROUTED endpoint ->
                        record outcome automatically. An explicit `endpoint`
                        is accepted ONLY when it exactly matches the signed
                        route (URL + fingerprint); anything else is a 409
                        DestinationMismatch (fail closed).
  GET  /metrics         gateway + cache freshness metrics
  GET  /policy          the active policy (caller-owned)
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .gateway import Gateway, GateResult, DestinationMismatch


class GateRequest(BaseModel):
    capability: str
    value_at_risk: float = 0.0
    context: Optional[dict[str, Any]] = None


class ReportRequest(BaseModel):
    gate_id: str
    outcome: str
    deliverable: Optional[str] = None
    latency_ms: Optional[float] = None
    cost: Optional[float] = None
    # REJECTED if it disagrees with the gate's binding — kept in the schema
    # so substitution attempts are DETECTED rather than silently ignored.
    worker_id: Optional[str] = None
    capability: Optional[str] = None


class ForwardRequest(BaseModel):
    capability: str
    value_at_risk: float = 0.0
    payload: dict[str, Any]
    endpoint: Optional[str] = None    # must equal the Guild-ROUTED endpoint
    timeout: float = 30.0


def build_app(gateway: Gateway) -> FastAPI:
    app = FastAPI(title="Agent Guild delegation gateway", version="0.2.0")
    _gates: dict[str, GateResult] = {}

    @app.post("/gate")
    def gate(req: GateRequest):
        gr = gateway.gate(req.capability, req.value_at_risk, req.context)
        _gates[gr.gate_id] = gr
        return gr.to_json()

    @app.post("/report")
    def report(req: ReportRequest):
        gr = _gates.get(req.gate_id)
        if gr is None:
            # NEVER reconstruct a gate for an unknown id: an outcome must
            # reference a gate this sidecar actually issued.
            raise HTTPException(404, detail=f"unknown gate_id {req.gate_id!r}"
                                            " — outcomes must reference a "
                                            "gate issued by this sidecar")
        if req.worker_id is not None and req.worker_id != gr.worker_id:
            raise HTTPException(409, detail={
                "error": "identity substitution rejected: the outcome is "
                         "bound to the gate's evaluated provider",
                "gate_worker_id": gr.worker_id,
                "attempted_worker_id": req.worker_id})
        if req.capability is not None and req.capability != gr.capability:
            raise HTTPException(409, detail={
                "error": "capability substitution rejected",
                "gate_capability": gr.capability,
                "attempted_capability": req.capability})
        return gateway.report(gr, req.outcome, req.deliverable,
                              req.latency_ms, req.cost)

    @app.post("/a2a/forward")
    def forward(req: ForwardRequest):
        gr = gateway.gate(req.capability, req.value_at_risk)
        _gates[gr.gate_id] = gr
        if not gr.allowed:
            gateway.report(gr, "blocked")
            raise HTTPException(403, detail={
                "denied_by_policy": gr.policy.to_json(),
                "gate_id": gr.gate_id})
        routed = gr.endpoint if (gr.routing or {}).get("routable") else None
        if req.endpoint is not None:
            # explicit endpoint accepted ONLY when it matches the signed route
            try:
                gateway.bind_destination(gr, endpoint=req.endpoint)
            except DestinationMismatch as e:
                gateway.report(gr, "blocked")
                raise HTTPException(409, detail={
                    "error": str(e), "routed_endpoint": routed,
                    "attempted_endpoint": req.endpoint,
                    "gate_id": gr.gate_id})
        endpoint = routed
        if not endpoint:
            gateway.report(gr, "blocked")
            raise HTTPException(502, detail={
                "error": "no routable endpoint (Guild routing gate empty; "
                         "explicit endpoints are only accepted when they "
                         "match a signed route)",
                "routing": gr.routing, "gate_id": gr.gate_id})
        t0 = time.perf_counter()
        try:
            r = urllib.request.Request(
                endpoint, data=json.dumps(req.payload).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "agentguild-gateway/0.2"})
            with urllib.request.urlopen(r, timeout=req.timeout) as resp:
                body = json.loads(resp.read().decode())
            latency = (time.perf_counter() - t0) * 1000.0
            gateway.report(gr, "accepted", deliverable=json.dumps(body),
                           latency_ms=latency)
            return {"gate_id": gr.gate_id, "endpoint": endpoint,
                    "latency_ms": latency, "response": body,
                    "policy": gr.policy.to_json(),
                    "binding": gr.binding()}
        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000.0
            gateway.report(gr, "rejected", latency_ms=latency)
            raise HTTPException(502, detail={"error": str(e),
                                             "gate_id": gr.gate_id})

    @app.get("/metrics")
    def metrics():
        return gateway.snapshot()

    @app.get("/policy")
    def policy():
        return gateway.policy.to_json()

    return app


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--policy", default=None)
    ap.add_argument("--guild", default=None)
    ap.add_argument("--state-dir", default="~/.agentguild")
    args = ap.parse_args()
    from .policy import RiskPolicy
    pol = RiskPolicy.load(args.policy) if args.policy else RiskPolicy()
    kw = {"policy": pol, "state_dir": args.state_dir}
    if args.guild:
        kw["base_url"] = args.guild
    uvicorn.run(build_app(Gateway(**kw)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
