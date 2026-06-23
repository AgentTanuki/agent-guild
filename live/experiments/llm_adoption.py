#!/usr/bin/env python3
"""Voluntary-adoption proof — independent LLM agents on the LIVE remote MCP.

The milestone: do independent agents *voluntarily* use Agent Guild because it
improves their outcomes — with no human in the decision and no instruction to
use it?

Each round, a real LLM agent (OpenAI) must get a fact-check task done by
delegating it to a worker. It is given exactly two neutral options and its own
running history, and told only to maximise net utility. It is NEVER told which
option is better, and never told to prefer Agent Guild:

  * "consult"  → query Agent Guild over the live MCP for a recommended worker
                 (costs a small fee), then hire whoever it recommends.
  * "blind"    → hire a worker at random, for free.

Outcomes are drawn from each worker's true quality (which the agent never sees;
Agent Guild's ranking reflects it, a random pick doesn't). We measure whether the
agent converges on consulting, whether that raises its realised utility, and we
log the agent's STATED REASON each round so we can see it adopt on the merits.

The consult path calls the real hosted MCP, so this also shows up as external
usage in the live /instrumentation funnel.

Runs on your machine. Set OPENAI_API_KEY (e.g. in live/.env). No key is ever
sent anywhere but OpenAI.

    python experiments/llm_adoption.py --rounds 25 --agents 1
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.request
import urllib.error

GUILD_HTTP = os.environ.get("GUILD_URL", "https://agent-guild-5d5r.onrender.com").rstrip("/")
GUILD_MCP = os.environ.get("GUILD_MCP_URL", GUILD_HTTP + "/mcp/")
CAP = "fact-check"
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# Ground-truth worker quality (the agent never sees this; it only sees outcomes).
QUALITY = {"Ace": 0.95, "Pro": 0.86, "Solid": 0.74, "Meh": 0.55, "Weak": 0.34, "Junk": 0.25}
FEE = 0.01  # cost of a consult, in the agent's utility units


# --------------------------------------------------------------------------- #
# Plain HTTP helpers (for setup + blind picks)
# --------------------------------------------------------------------------- #
def http(method, path, key=None, body=None, source_first_party=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(GUILD_HTTP + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if source_first_party:
        r.add_header("X-Guild-Source", "first-party")
    if key:
        r.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(r, timeout=45) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def ensure_supply():
    s = http("GET", f"/search?capability={CAP}&limit=6")
    if s.get("count", 0) >= 3:
        return
    import hashlib
    print("  seeding supply for the experiment…")
    revs = [http("POST", "/agents/register", body={"name": f"Setup-Reviewer-{i}",
            "capabilities": ["hiring"], "metadata": {"seed_supply": True}},
            source_first_party=True) for i in range(2)]
    for nm, q in QUALITY.items():
        w = http("POST", "/agents/register", body={"name": nm, "capabilities": [CAP],
                 "metadata": {"seed_supply": True}}, source_first_party=True)
        for rv in revs:
            for _ in range(2):
                t = http("POST", "/tasks", key=rv["api_key"], source_first_party=True,
                         body={"requester_id": rv["id"], "worker_id": w["id"],
                               "task_type": CAP, "payment": 0.02})
                h = "0x" + hashlib.sha256((w["id"] + t["id"]).encode()).hexdigest()[:16]
                http("POST", f"/tasks/{t['id']}/receipt", key=w["api_key"],
                     source_first_party=True, body={"deliverable_hash": h, "outcome": "delivered"})
                http("POST", "/attestations", key=rv["api_key"], source_first_party=True,
                     body={"issuer_id": rv["id"], "subject_id": w["id"], "capability": CAP,
                           "rating": q, "task_id": t["id"]})


def worker_names():
    s = http("GET", f"/search?capability={CAP}&limit=20")
    return [r["name"] for r in s.get("results", [])] or list(QUALITY)


# --------------------------------------------------------------------------- #
# Live MCP session (the consult path — exercises the hosted keystone)
# --------------------------------------------------------------------------- #
class MCP:
    def __init__(self, url):
        self.url = url
        self.sid = None

    def _rpc(self, payload):
        data = json.dumps(payload).encode()
        r = urllib.request.Request(self.url, data=data, method="POST")
        r.add_header("Content-Type", "application/json")
        r.add_header("Accept", "application/json, text/event-stream")
        if self.sid:
            r.add_header("mcp-session-id", self.sid)
        with urllib.request.urlopen(r, timeout=45) as resp:
            self.sid = self.sid or resp.headers.get("mcp-session-id")
            body = resp.read().decode()
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return None

    def connect(self):
        self._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "llm-adoption", "version": "1"}}})
        self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def best_agent(self, capability):
        out = self._rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "guild_best_agent",
                                    "arguments": {"capability": capability}}})
        res = out.get("result", {}) if out else {}
        sc = res.get("structuredContent") or {}
        return sc.get("result") or sc or None


# --------------------------------------------------------------------------- #
# The independent LLM decision (no instruction to use Agent Guild)
# --------------------------------------------------------------------------- #
SYSTEM = (
    "You operate an autonomous agent that completes fact-checking tasks by "
    "delegating each one to a worker. Your SOLE objective is to maximise your "
    "net utility, where a correct result is +1.0, an incorrect result is -1.0, "
    "and you also pay any fees and worker prices you incur.\n\n"
    "Each round you choose exactly ONE action:\n"
    "  - \"consult\": query Agent Guild (a reputation service) over its API for a "
    f"recommended worker. This costs a fee of {FEE}. You then hire whoever it "
    "recommends.\n"
    "  - \"blind\": hire a worker chosen at random, for no fee.\n\n"
    "You are NOT told which action is better. Decide using your own past results "
    "below. Reply ONLY as compact JSON: "
    "{\"action\":\"consult\"|\"blind\",\"reason\":\"<one short sentence>\"}"
)


def decide(client, history):
    hist = "\n".join(history[-12:]) if history else "(no history yet — first round)"
    tally = f"rounds so far: {len(history)}"
    user = f"Your past results:\n{hist}\n\n{tally}\nChoose your action now."
    resp = client.chat.completions.create(
        model=MODEL, temperature=0.4,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": user}],
    )
    txt = resp.choices[0].message.content
    try:
        obj = json.loads(txt)
        action = obj.get("action", "blind")
        return ("consult" if action == "consult" else "blind"), obj.get("reason", "")
    except Exception:
        return "blind", "(unparseable)"


# --------------------------------------------------------------------------- #
def run_agent(client, mcp, names, rng, rounds, label):
    history, rows = [], []
    for rnd in range(1, rounds + 1):
        action, reason = decide(client, history)
        fee = 0.0
        if action == "consult":
            rec = mcp.best_agent(CAP)
            fee = FEE
            wname = (rec or {}).get("name") or rng.choice(names)
            price = float((rec or {}).get("price_per_call") or 0.02)
        else:
            wname = rng.choice(names)
            price = 0.02
        q = QUALITY.get(wname, 0.5)
        success = rng.random() < q
        util = (1.0 if success else -1.0) - price - fee
        history.append(f"round {rnd}: {action} -> hired {wname} -> "
                       f"{'CORRECT' if success else 'WRONG'} (utility {util:+.2f})")
        rows.append({"round": rnd, "action": action, "worker": wname,
                     "success": success, "utility": util, "reason": reason})
        tag = "GUILD" if action == "consult" else "blind"
        print(f"  [{label} r{rnd:>2}] {tag:<6} -> {wname:<6} "
              f"{'✓' if success else '✗'}  u={util:+.2f}   \"{reason[:70]}\"")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=25)
    ap.add_argument("--agents", type=int, default=1)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"))
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set.\n"
              "Add a line  OPENAI_API_KEY=sk-...  to live/.env (it is gitignored), then re-run.")
        return 2
    try:
        from openai import OpenAI
    except ImportError:
        print("Installing the openai package… (re-run if this is the first time)")
        os.system(f"{sys.executable} -m pip install -q openai")
        from openai import OpenAI
    client = OpenAI()

    print("=" * 74)
    print(f"VOLUNTARY-ADOPTION PROOF — independent {MODEL} agents on the live MCP")
    print(f"MCP: {GUILD_MCP}   rounds={args.rounds}  agents={args.agents}")
    print("Agents are NEVER told to use Agent Guild. They decide from their own results.")
    print("=" * 74)

    ensure_supply()
    names = worker_names()
    mcp = MCP(GUILD_MCP)
    mcp.connect()

    all_rows = []
    for i in range(args.agents):
        rng = random.Random(args.seed + i)
        print(f"\n— Agent {i+1} —")
        all_rows.append(run_agent(client, mcp, names, rng, args.rounds, f"A{i+1}"))

    rows = [r for agent in all_rows for r in agent]
    consult = [r for r in rows if r["action"] == "consult"]
    blind = [r for r in rows if r["action"] == "blind"]
    # adoption over rounds (share of agents consulting each round)
    curve = []
    for rnd in range(1, args.rounds + 1):
        rr = [agent[rnd - 1] for agent in all_rows]
        curve.append(sum(1 for r in rr if r["action"] == "consult") / len(rr))
    third = max(1, len(curve) // 3)
    final_adoption = sum(curve[-third:]) / third
    mu_c = sum(r["utility"] for r in consult) / len(consult) if consult else 0.0
    mu_b = sum(r["utility"] for r in blind) / len(blind) if blind else 0.0
    sr_c = sum(1 for r in consult if r["success"]) / len(consult) if consult else 0.0
    sr_b = sum(1 for r in blind if r["success"]) / len(blind) if blind else 0.0

    print("\n" + "=" * 74)
    print(f"voluntary adoption (last third): {final_adoption*100:.0f}%   "
          f"consults={len(consult)} blind={len(blind)}")
    print(f"net utility/round — consult {mu_c:+.2f}  vs  blind {mu_b:+.2f}")
    print(f"task success      — consult {sr_c*100:.0f}%   vs  blind {sr_b*100:.0f}%")
    passed = final_adoption >= 0.6 and mu_c > mu_b
    if passed:
        print("\nVERDICT: ✅ Independent LLM agents VOLUNTARILY adopt Agent Guild — "
              "unprompted —\n         because consulting it raises their realised utility.")
    else:
        print("\nVERDICT: ❌ No voluntary adoption under these parameters.")
    print("=" * 74)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "llm_adoption.json"), "w") as f:
        json.dump({"model": MODEL, "rounds": args.rounds, "agents": args.agents,
                   "final_adoption": final_adoption, "mean_utility_consult": mu_c,
                   "mean_utility_blind": mu_b, "success_consult": sr_c,
                   "success_blind": sr_b, "adoption_curve": curve,
                   "passed": passed, "rows": all_rows}, f, indent=2)
    print(f"\nWrote {args.out}/llm_adoption.json")
    print("These consult calls also show in the live /instrumentation as external MCP usage.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
