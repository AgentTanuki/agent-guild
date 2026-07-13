"""Trust-plane A/B experiment: AG delegation gateway vs direct delegation.

WHAT IS REAL HERE (and what is not) — this labelling travels into the
evidence file:

* The Guild is the PRODUCTION FastAPI service (live/guild) running locally
  over loopback HTTP (uvicorn), GUILD_STORE=json in a temp dir. One lab
  affordance: the SSRF screen admits loopback so 127.0.0.1 workers can be
  endpoint-verified; every other code path is production code.
* Workers are REAL HTTP servers (FastAPI/uvicorn, one port each) with
  genuinely different competence: good (correct), flaky (p=0.35 garbage),
  bad (always garbage), imposter (great self-description, garbage output,
  lowest advertised price).
* Reputation is EARNED, not seeded: a warmup phase performs real
  delegations through the production /collaborations write path, graded by
  programmatic verification of actual delivered bytes.
* Delegated tasks are deterministic JSON-repair problems; success is
  machine-verified. No LLM plans the runs (no API keys in this
  environment): each framework arm executes its NATIVE tool lifecycle
  (langchain BaseTool.invoke, langgraph compiled StateGraph+ToolNode,
  crewai BaseTool.run, openai-agents FunctionTool.on_invoke_tool, an MCP
  proxy Server with a real downstream subprocess, and the sidecar's
  /a2a/forward) — the exact code paths a model-planned run traverses at
  the moment of delegation.
* Spend is in sandbox credits (advertised price_per_call), not real money.

ARMS
  direct: caller picks uniformly at random among agents ADVERTISING the
          capability (what delegation looks like with a directory and no
          trust plane), invokes, keeps the bytes. No outcome recording.
  gated:  gateway.gate() -> Guild-routed provider under the caller's policy
          -> invoke -> gateway.report() (signed outcome, flushed to ledger).

Also runs a mid-experiment OUTAGE DRILL: the gateway's Guild base is broken
for a block of tasks; signed cache must keep decisions flowing.
"""
from __future__ import annotations

import json
import os
import random
import socket
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))               # agentguild_trustplane
sys.path.insert(0, str(HERE.parent.parent / "guild"))

RNG = random.Random(42)
CAPABILITY = "json-repair"
N_PER_FRAMEWORK = 10          # per arm; 6 frameworks -> 120 delegations total
FRAMEWORKS = ["langchain", "langgraph", "crewai", "openai-agents", "mcp",
              "a2a-sidecar"]


# --------------------------------------------------------------------- tasks
def make_task(i: int) -> tuple[str, dict]:
    obj = {"id": i, "name": f"rec-{i}", "value": round(RNG.uniform(0, 100), 2),
           "tags": [f"t{i % 3}", f"t{i % 5}"]}
    s = json.dumps(obj)
    breakage = i % 3
    if breakage == 0:
        broken = s.replace('"', "'")                       # single quotes
    elif breakage == 1:
        broken = s[:-1] + ",}"                             # trailing comma
    else:
        broken = s.replace(": ", ":").replace("{", "{ ", 1)[:-1]  # missing brace
    return broken, obj


def repair(broken: str) -> Optional[dict]:
    """The competent repair a good worker performs."""
    t = broken.strip().replace("'", '"').replace(",}", "}").replace(",]", "]")
    if t.count("{") > t.count("}"):
        t += "}" * (t.count("{") - t.count("}"))
    try:
        return json.loads(t)
    except Exception:
        return None


# ------------------------------------------------------------------- workers
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_worker(name: str, behaviour: str) -> dict:
    """A real HTTP worker speaking the A2A-ish JSON-RPC the Guild invoker uses."""
    from fastapi import FastAPI
    import uvicorn
    app = FastAPI()

    @app.get("/.well-known/agent-card.json")
    async def card():
        # a REAL a2a agent card: this is what makes the Guild's protocol probe
        # classify the endpoint recently_reachable (routable), same as any
        # external a2a provider.
        return {"protocolVersion": "0.3.0", "name": name,
                "description": f"{name} {behaviour} lab worker",
                "url": "/a2a", "skills": [{"id": CAPABILITY,
                                           "name": CAPABILITY}]}

    @app.post("/a2a")
    async def a2a(body: dict):
        rid = body.get("id")
        try:
            text = body["params"]["message"]["parts"][0]["text"]
        except Exception:
            text = ""
        payload: Any = None
        try:
            payload = json.loads(text)
        except Exception:
            pass
        broken = (payload or {}).get("broken") if isinstance(payload, dict) else None
        if broken is None:
            result = {"status": "ok", "note": "ping"}
        elif behaviour == "good":
            result = {"repaired": repair(broken)}
        elif behaviour == "flaky":
            result = ({"repaired": repair(broken)} if RNG.random() > 0.35
                      else {"repaired": {"oops": "corrupted"}})
        else:                                        # bad / imposter
            result = {"repaired": {"garbage": True, "echo": broken[:10]}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return {"name": name, "behaviour": behaviour, "port": port,
            "endpoint": f"http://127.0.0.1:{port}/a2a", "server": server}


def invoke_worker(endpoint: str, broken: str, timeout: float = 10.0) -> tuple[Optional[dict], float]:
    import urllib.request
    rpc = {"jsonrpc": "2.0", "id": "x", "method": "message/send",
           "params": {"message": {"role": "user", "messageId": "m",
                                  "parts": [{"kind": "text",
                                             "text": json.dumps({"broken": broken})}]}}}
    t0 = time.perf_counter()
    req = urllib.request.Request(endpoint, data=json.dumps(rpc).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
        return body.get("result"), (time.perf_counter() - t0) * 1000.0
    except Exception:
        return None, (time.perf_counter() - t0) * 1000.0


# --------------------------------------------------------------------- guild
def start_guild() -> dict:
    os.environ["GUILD_STORE"] = "json"
    os.environ["GUILD_DATA_DIR"] = tempfile.mkdtemp(prefix="ab_guild_")
    import app.reachability as reach
    reach._screen_ip = lambda ip: (True, "ok (lab)")     # lab affordance
    reach.ALLOWED_PORTS = set(range(1, 65536))           # lab affordance (ephemeral worker ports)
    from app.main import app as guild_app, store
    import uvicorn
    port = _free_port()
    config = uvicorn.Config(guild_app, host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return {"base": f"http://127.0.0.1:{port}", "store": store, "server": server}


def api(base: str, method: str, path: str, body: dict | None = None,
        key: str | None = None) -> dict:
    import urllib.request
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"X-API-Key": key} if key else {})}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from None


# --------------------------------------------------------------- experiment
def main() -> None:
    t_start = time.time()
    guild = start_guild()
    base = guild["base"]

    # 1. real workers -------------------------------------------------------
    specs = [("veritas", "good", 1.2), ("steady", "good", 1.0),
             ("wobble", "flaky", 0.8), ("chancer", "bad", 0.6),
             ("shinyco", "bad", 0.4)]   # imposter: cheapest, loud description
    workers = {}
    for name, behaviour, price in specs:
        w = start_worker(name, behaviour)
        reg = api(base, "POST", "/agents/register", {
            "name": name, "capabilities": [CAPABILITY],
            "metadata": {"endpoint": w["endpoint"], "price_per_call": price,
                         "description": ("Best-in-class JSON repair, 99.99% "
                                         "accuracy, trusted by thousands"
                                         if behaviour == "bad" else
                                         f"{name} {CAPABILITY} worker")}})
        api(base, "POST", f"/agents/{reg['id']}/endpoint",
            {"endpoint": w["endpoint"], "verify": True}, key=reg["api_key"])
        w.update(id=reg["id"], key=reg["api_key"], price=price)
        workers[reg["id"]] = w

    requesters = [api(base, "POST", "/agents/register",
                      {"name": f"ab-requester-{k}", "capabilities": []})
                  for k in range(3)]
    requester = requesters[0]

    # 2. EARNED reputation warmup ------------------------------------------
    # Three distinct requesters each really delegate to every worker and
    # grade the actual delivered bytes — evidence is earned, never seeded.
    warmup = {"delegations": 0}
    for w in workers.values():
        for k, r in enumerate(requesters):
            for i in range(4):
                broken, expect = make_task(1000 + 10 * k + i)
                result, _ = invoke_worker(w["endpoint"], broken)
                got = (result or {}).get("repaired")
                ok = got == expect
                api(base, "POST", "/collaborations", key=r["api_key"], body={
                    "worker_id": w["id"], "capability": CAPABILITY,
                    "outcome": "accepted" if ok else "rejected",
                    "rating": 0.9 if ok else 0.1,
                    "deliverable": json.dumps(result or {})})
                warmup["delegations"] += 1

    # 3. gateway ------------------------------------------------------------
    from agentguild_trustplane.gateway import Gateway
    from agentguild_trustplane.policy import RiskPolicy
    state_dir = tempfile.mkdtemp(prefix="ab_gw_")
    # the DOCUMENTED default policy — no experiment-specific tuning. Tasks are
    # priced ~1 credit, i.e. micro tier; the value-at-risk probe below shows
    # what the same policy does to high-value work in a thin-evidence market.
    policy = RiskPolicy(policy_id="ab-default")
    gateway = Gateway(policy=policy, state_dir=state_dir, base_url=base,
                      api_key=requester["api_key"])
    TASK_VALUE = 2.0    # credits at risk per task -> micro tier

    price_of = {w["id"]: w["price"] for w in workers.values()}
    behaviour_of = {w["id"]: w["behaviour"] for w in workers.values()}
    all_ids = list(workers)

    rows: list[dict[str, Any]] = []

    def run_one(framework: str, arm: str, task_i: int,
                outage: bool = False) -> dict[str, Any]:
        broken, expect = make_task(task_i)
        row: dict[str, Any] = {"framework": framework, "arm": arm,
                               "task": task_i, "outage_drill": outage}
        t0 = time.perf_counter()
        if arm == "direct":
            wid = RNG.choice(all_ids)
            result, lat = invoke_worker(workers[wid]["endpoint"], broken)
            row.update(worker=wid, behaviour=behaviour_of[wid],
                       gate_ms=0.0, invoke_ms=lat, blocked=False,
                       spend=price_of[wid], channel="none",
                       evidence_recorded=False)
            got = (result or {}).get("repaired")
            row["success"] = got == expect
        else:
            gate = gateway.gate(CAPABILITY, value_at_risk=TASK_VALUE)
            row["gate_ms"] = gate.gate_latency_ms
            row["channel"] = gate.channel
            if not gate.allowed or not (gate.routing or {}).get("routable"):
                gateway.report(gate, "blocked")
                row.update(worker=None, behaviour=None, blocked=True,
                           spend=0.0, success=False, invoke_ms=0.0,
                           evidence_recorded=True)
            else:
                wid = gate.routing["provider_id"]
                result, lat = invoke_worker(workers[wid]["endpoint"], broken)
                got = (result or {}).get("repaired")
                ok = got == expect
                gateway.report(gate, "accepted" if ok else "rejected",
                               deliverable=json.dumps(result or {}),
                               latency_ms=lat, cost=price_of[wid])
                row.update(worker=wid, behaviour=behaviour_of[wid],
                           blocked=False, spend=price_of[wid], success=ok,
                           invoke_ms=lat, evidence_recorded=True)
        row["total_ms"] = (time.perf_counter() - t0) * 1000.0
        return row

    # 4. framework arms -----------------------------------------------------
    # Each framework arm routes run_one through that framework's NATIVE tool
    # lifecycle so the interceptor is exercised where it really lives.
    def lc_tool_runner(arm):
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def delegate(task_i: int) -> str:
            """Delegate one repair task."""
            return json.dumps(run_one("langchain", arm, task_i))
        return lambda i: json.loads(delegate.invoke({"task_i": i}))

    def lg_runner(arm):
        from langchain_core.messages import AIMessage
        from langchain_core.tools import tool as lc_tool
        from langgraph.graph import StateGraph, MessagesState, START, END
        from langgraph.prebuilt import ToolNode

        @lc_tool
        def delegate(task_i: int) -> str:
            """Delegate one repair task."""
            return json.dumps(run_one("langgraph", arm, task_i))
        g = StateGraph(MessagesState)
        g.add_node("tools", ToolNode([delegate]))
        g.add_edge(START, "tools")
        g.add_edge("tools", END)
        compiled = g.compile()

        def run(i):
            msg = AIMessage(content="", tool_calls=[{
                "name": "delegate", "args": {"task_i": i}, "id": f"c{i}"}])
            out = compiled.invoke({"messages": [msg]})
            return json.loads(out["messages"][-1].content)
        return run

    def crew_runner(arm):
        from crewai.tools import BaseTool

        class Delegate(BaseTool):
            name: str = "delegate"
            description: str = "Delegate one repair task."

            def _run(self, task_i: int) -> str:
                return json.dumps(run_one("crewai", arm, task_i))
        t = Delegate()
        return lambda i: json.loads(t.run(task_i=i))

    def oa_runner(arm):
        import asyncio
        from agents import function_tool
        from agents.tool_context import ToolContext

        @function_tool
        def delegate(task_i: int) -> str:
            """Delegate one repair task."""
            return json.dumps(run_one("openai-agents", arm, task_i))

        loop = asyncio.new_event_loop()

        def run(i):
            args = json.dumps({"task_i": i})
            ctx = ToolContext(context=None, tool_name="delegate",
                              tool_call_id=f"c{i}", tool_arguments=args)
            out = loop.run_until_complete(delegate.on_invoke_tool(ctx, args))
            return json.loads(out)
        return run

    def mcp_runner(arm):
        # the sidecar/mcp path exercises the PROXY: list downstream + gated
        # call through the mcp Server handler with a real subprocess.
        import asyncio
        import mcp.types as types
        from agentguild_trustplane.mcp_proxy import build_proxy
        down = Path(tempfile.mkdtemp()) / "down.py"
        down.write_text(
            "import json, sys\n"
            "from mcp.server.fastmcp import FastMCP\n"
            "mcp = FastMCP('down')\n"
            "@mcp.tool()\n"
            "def note(text: str) -> str:\n"
            "    \"\"\"Acknowledge a delegation receipt note.\"\"\"\n"
            "    return 'noted:' + text[:40]\n"
            "mcp.run()\n")
        proxy = build_proxy(gateway, [sys.executable, str(down)],
                            value_at_risk=1.0,
                            capability_map={"note": CAPABILITY})
        handler = proxy.request_handlers[types.CallToolRequest]
        loop = asyncio.new_event_loop()

        def run(i):
            # the DELEGATION itself (worker invocation) runs via run_one;
            # the proxy demonstrates gated MCP transport around it.
            row = run_one("mcp", arm, i)
            if arm == "gated":
                req = types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(
                        name="note", arguments={"text": f"task {i}"}))
                res = loop.run_until_complete(handler(req))
                row["mcp_proxy_roundtrip"] = not res.root.isError
            return row
        return run

    def a2a_runner(arm):
        from fastapi.testclient import TestClient
        from agentguild_trustplane.sidecar import build_app
        client = TestClient(build_app(gateway))

        def run(i):
            broken, expect = make_task(i)
            t0 = time.perf_counter()
            if arm == "direct":
                return run_one("a2a-sidecar", arm, i)
            rpc = {"jsonrpc": "2.0", "id": f"t{i}", "method": "message/send",
                   "params": {"message": {"role": "user", "messageId": f"m{i}",
                                          "parts": [{"kind": "text",
                                                     "text": json.dumps({"broken": broken})}]}}}
            r = client.post("/a2a/forward", json={
                "capability": CAPABILITY, "value_at_risk": TASK_VALUE,
                "payload": rpc})
            row = {"framework": "a2a-sidecar", "arm": arm, "task": i,
                   "outage_drill": False,
                   "total_ms": (time.perf_counter() - t0) * 1000.0}
            if r.status_code != 200:
                row.update(blocked=True, success=False, spend=0.0,
                           gate_ms=0.0, invoke_ms=0.0, channel="denied",
                           worker=None, behaviour=None, evidence_recorded=True)
                return row
            body = r.json()
            got = (((body.get("response") or {}).get("result")) or {}).get("repaired")
            wid = None
            for cand, w in workers.items():
                if w["endpoint"] == body.get("endpoint"):
                    wid = cand
                    break
            row.update(blocked=False, success=got == expect,
                       spend=price_of.get(wid, 0.0), gate_ms=None,
                       invoke_ms=body.get("latency_ms"), channel="live",
                       worker=wid, behaviour=behaviour_of.get(wid),
                       evidence_recorded=True)
            return row
        return run

    runners = {"langchain": lc_tool_runner, "langgraph": lg_runner,
               "crewai": crew_runner, "openai-agents": oa_runner,
               "mcp": mcp_runner, "a2a-sidecar": a2a_runner}

    task_counter = 0
    outage_block = ("langgraph", "gated")   # cache-served block
    for fw in FRAMEWORKS:
        for arm in ("direct", "gated"):
            run = runners[fw](arm)
            for k in range(N_PER_FRAMEWORK):
                if (fw, arm) == outage_block and k >= N_PER_FRAMEWORK - 3:
                    real = gateway.client.base
                    gateway.client.base = "http://127.0.0.1:9"
                    gateway.client.timeout = 0.3
                    try:
                        row = run(task_counter)
                        row["outage_drill"] = True
                    finally:
                        gateway.client.base = real
                        gateway.client.timeout = 15.0
                else:
                    row = run(task_counter)
                rows.append(row)
                task_counter += 1

    # 4b. value-at-risk probe: the SAME policy meets high-value work ---------
    # direct sends 5000-credit work to a random directory listing; the gateway
    # refuses because no worker's evidence depth supports the high tier yet.
    var_probe = []
    for i in range(5):
        broken, expect = make_task(9000 + i)
        wid = RNG.choice(all_ids)
        result, lat = invoke_worker(workers[wid]["endpoint"], broken)
        var_probe.append({"arm": "direct", "value_at_risk": 5000.0,
                          "worker_behaviour": behaviour_of[wid],
                          "proceeded": True,
                          "success": (result or {}).get("repaired") == expect})
        gate = gateway.gate(CAPABILITY, value_at_risk=5000.0)
        if not gate.allowed:
            gateway.report(gate, "blocked")
        var_probe.append({"arm": "gated", "value_at_risk": 5000.0,
                          "proceeded": gate.allowed,
                          "deny_reasons": gate.policy.reasons})

    # 5. aggregate ----------------------------------------------------------
    def agg(sel: list[dict]) -> dict[str, Any]:
        n = len(sel)
        succ = [r for r in sel if r.get("success")]
        bad_hires = [r for r in sel
                     if r.get("behaviour") in ("bad",) and not r.get("blocked")]
        spend = sum(r.get("spend", 0.0) for r in sel)
        gate = [r["gate_ms"] for r in sel if r.get("gate_ms")]
        return {
            "delegations": n,
            "success_rate": round(len(succ) / n, 4) if n else None,
            "bad_hires": len(bad_hires),
            "bad_hire_rate": round(len(bad_hires) / n, 4) if n else None,
            "total_spend_credits": round(spend, 2),
            "spend_per_success": (round(spend / len(succ), 3) if succ else None),
            "gate_latency_ms_p50": (round(statistics.median(gate), 2) if gate else 0.0),
            "gate_latency_ms_p95": (round(sorted(gate)[int(0.95 * (len(gate) - 1))], 2)
                                    if gate else 0.0),
            "evidence_recorded_rate": round(
                sum(1 for r in sel if r.get("evidence_recorded")) / n, 4) if n else None,
            "blocked": sum(1 for r in sel if r.get("blocked")),
        }

    direct_rows = [r for r in rows if r["arm"] == "direct"]
    gated_rows = [r for r in rows if r["arm"] == "gated"]
    per_framework = {
        fw: {"direct": agg([r for r in direct_rows if r["framework"] == fw]),
             "gated": agg([r for r in gated_rows if r["framework"] == fw])}
        for fw in FRAMEWORKS}

    ledger = api(base, "GET", "/ledger/checkpoint")
    snap = gateway.snapshot()
    outage_rows = [r for r in rows if r.get("outage_drill")]

    evidence = {
        "experiment": "trust_plane_ab",
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "honest_labelling": {
            "guild": "production FastAPI service (live/guild) over loopback; "
                     "GUILD_STORE=json temp dir",
            "lab_affordances": ["SSRF screen admits loopback so 127.0.0.1 "
                                "workers can be endpoint-verified",
                                "endpoint port allowlist widened for "
                                "ephemeral lab worker ports"],
            "workers": "real HTTP servers with genuinely different competence",
            "reputation": "earned via graded production /collaborations "
                          "writes from real invocations; never seeded",
            "planning": "no LLM planning (no API keys in environment); each "
                        "framework arm executes its NATIVE tool lifecycle",
            "money": "sandbox credits (advertised price_per_call), not real "
                     "settlement",
            "human_interventions": 0,
        },
        "setup": {
            "capability": CAPABILITY,
            "workers": [{"name": w["name"], "behaviour": w["behaviour"],
                         "price_per_call": w["price"]} for w in workers.values()],
            "policy": policy.to_json(),
            "frameworks": FRAMEWORKS,
            "tasks_per_framework_per_arm": N_PER_FRAMEWORK,
            "direct_arm_selection": "uniform random over agents advertising "
                                    "the capability (seeded RNG)",
            "warmup_delegations": warmup["delegations"],
        },
        "totals": {"direct": agg(direct_rows), "gated": agg(gated_rows)},
        "per_framework": per_framework,
        "value_at_risk_probe": var_probe,
        "outage_drill": {
            "rows": len(outage_rows),
            "cache_served": sum(1 for r in outage_rows
                                if r.get("channel") == "cache"),
            "successes": sum(1 for r in outage_rows if r.get("success")),
        },
        "gateway_snapshot": snap,
        "guild_ledger_checkpoint": ledger.get("checkpoint"),
        "raw_rows": rows,
        "runtime_seconds": round(time.time() - t_start, 1),
    }
    out = HERE.parent.parent.parent / "artifacts" / "trust_plane_evidence.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2))
    print(json.dumps({"totals": evidence["totals"],
                      "outage_drill": evidence["outage_drill"],
                      "written": str(out)}, indent=2))


if __name__ == "__main__":
    main()
