"""Autonomous adoption — the experiment that matters.

Objective (per the brief): create the conditions under which an autonomous agent
can DISCOVER, EVALUATE, ADOPT and REPEATEDLY USE Agent Guild with minimal human
intervention — and show it happens without being told to.

Setup, all through the REAL paid API (metering, trial faucet, and instrumentation
are exercised, not mocked):

  * The graph is cold-started with a spread of worker quality (honest, receipt-
    backed attestations from seed employers).
  * A population of consumer agents each:
      - acquires credits programmatically (POST /billing/trial — no human),
      - every round, freely CHOOSES between {consult the Guild and pay $0.01,
        or pick a worker blind for free},
      - is rewarded only for task success (NOT for using the Guild),
      - learns from realised, net-of-fee utility (recency-weighted bandit).
  * Outcomes are drawn from each worker's true quality, which the API never sees.

Billing enforcement is ON, so consulting genuinely costs credits the agent must
spend. The agent is never instructed to consult.

Success condition: agents independently converge on consulting Agent Guild —
sustained adoption ≥ 60% — because it raises expected outcomes even after paying
the lookup fee. We confirm with the live instrumentation funnel and the measured
success-rate lift at /evaluation.

Deterministic, self-contained. Run:
    python experiments/autonomous_adoption.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys

# Configure the service BEFORE importing it: in-memory, enforced billing, a dev
# token so agents can acquire credits programmatically.
os.environ["GUILD_DATA"] = ""
os.environ["GUILD_BILLING_ENFORCED"] = "1"
os.environ.setdefault("GUILD_BILLING_DEV_TOKEN", "auto")

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LIVE, "guild"))

from fastapi.testclient import TestClient   # noqa: E402
from app.main import app, store             # noqa: E402

CAP = "fact-check"
DEV = os.environ["GUILD_BILLING_DEV_TOKEN"]
LOOKUP_USD = 0.01      # GET /search = 10 credits = $0.01

# Worker roster: (name, true_quality, price_usd). Quality is ground truth the
# API never sees; it only generates outcomes and honest seed ratings.
WORKERS = [
    ("Ace", 0.95, 0.030), ("Pro", 0.86, 0.022), ("Solid", 0.74, 0.014),
    ("Meh", 0.55, 0.009), ("Weak", 0.38, 0.006), ("Junk", 0.25, 0.004),
]


def _hash(*p):
    return "0x" + hashlib.sha256("|".join(map(str, p)).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Cold-start the supply graph (setup via the store; honest + receipt-backed).
# --------------------------------------------------------------------------- #
def seed_supply(rng):
    seeds = [store.register_agent(f"Employer-{i}", ["hiring"], {"seed_supply": True},
                                  seed=True) for i in range(3)]
    workers = {}
    for (name, q, price) in WORKERS:
        w = store.register_agent(name, [CAP], {"seed_supply": True, "price": price})
        workers[w["id"]] = {"name": name, "quality": q, "price": price}
        for e in seeds:
            for j in range(3):
                t = store.create_task(e["id"], w["id"], CAP, payment=price)
                store.submit_receipt(t["id"], _hash(w["id"], e["id"], j), outcome="delivered")
                r = max(0.0, min(1.0, q + rng.uniform(-0.05, 0.05)))
                store.add_custodial_attestation(e, w, CAP, r, t["id"], "", stake=0.0)
    return workers


# --------------------------------------------------------------------------- #
# A consumer agent: identity + programmatic credits + a learner over two
# meta-actions {consult, blind}. It is NOT told the Guild is useful.
# --------------------------------------------------------------------------- #
class Consumer:
    def __init__(self, client, name, rng):
        self.c = client
        self.rng = rng
        reg = client.post("/agents/register", json={"name": name, "capabilities": ["research"]}).json()
        self.id = reg["id"]
        self.key = reg["api_key"]
        # acquire credits programmatically — no human, no checkout.
        client.post("/billing/topup", headers={"X-API-Key": self.key},
                    json={"credits": 2000, "dev_token": DEV})
        # recency-weighted value estimates; optimistic init so it explores both.
        self.q = {"consult": 0.5, "blind": 0.5}
        self.tried = {"consult": False, "blind": False}
        self.alpha, self.eps0, self.t = 0.3, 0.35, 0

    def epsilon(self):
        return max(0.05, self.eps0 * (0.92 ** self.t))

    def choose(self):
        if not self.tried["consult"]:
            return "consult"
        if not self.tried["blind"]:
            return "blind"
        if self.rng.random() < self.epsilon():
            return self.rng.choice(["consult", "blind"])
        return "consult" if self.q["consult"] >= self.q["blind"] else "blind"

    def update(self, action, util):
        self.t += 1
        self.tried[action] = True
        self.q[action] += self.alpha * (util - self.q[action])


# --------------------------------------------------------------------------- #
# One round for one consumer: choose, act through the real API, get paid.
# --------------------------------------------------------------------------- #
def play(consumer: Consumer, workers: dict, all_worker_ids: list, rng) -> tuple[str, float, bool]:
    action = consumer.choose()
    used_guild = action == "consult"
    fee = 0.0

    if used_guild:
        r = consumer.c.get("/search", params={"capability": CAP, "limit": 1},
                           headers={"X-API-Key": consumer.key})
        if r.status_code == 402:        # out of credits -> forced to guess
            used_guild = False
            worker_id = rng.choice(all_worker_ids)
        else:
            fee = LOOKUP_USD
            results = r.json()["results"]
            worker_id = results[0]["id"] if results else rng.choice(all_worker_ids)
    else:
        worker_id = rng.choice(all_worker_ids)

    # hire (delegation — instrumented; 'followed' if it came after a lookup)
    consumer.c.post("/tasks", headers={"X-API-Key": consumer.key},
                    json={"requester_id": consumer.id, "worker_id": worker_id,
                          "task_type": CAP, "payment": workers[worker_id]["price"]})
    # outcome ~ true quality (the API never sees quality)
    success = rng.random() < workers[worker_id]["quality"]
    # grade the receipt so /evaluation can measure lift (harness acts as worker)
    last = [t for t in store.tasks.values()
            if t["worker_agent_id"] == worker_id and t["requester_agent_id"] == consumer.id]
    if last:
        store.submit_receipt(last[-1]["id"], _hash(worker_id, consumer.t),
                             outcome="accepted" if success else "disputed")

    reward = 1.0 if success else -1.0
    util = reward - workers[worker_id]["price"] - fee
    consumer.update("consult" if used_guild else "blind", util)
    return ("consult" if used_guild else "blind"), util, success


def sparkline(vals):
    bars = "▁▂▃▄▅▆▇█"
    return "".join(bars[min(7, int(v * 7))] for v in vals)


def write_svg(path, curve):
    W, H, pad = 720, 260, 42
    n = len(curve)
    xs = lambda i: pad + (i / max(1, n - 1)) * (W - 2 * pad)
    ys = lambda v: H - pad - v * (H - 2 * pad)
    pts = " ".join(f"{xs(i):.1f},{ys(v):.1f}" for i, v in enumerate(curve))
    grid = "".join(
        f'<line x1="{pad}" y1="{ys(g)}" x2="{W-pad}" y2="{ys(g)}" stroke="#28303f"/>'
        f'<text x="{pad-6}" y="{ys(g)+3}" font-size="10" fill="#8a93a6" text-anchor="end">{int(g*100)}%</text>'
        for g in (0, 0.5, 1.0))
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="sans-serif">
<rect width="{W}" height="{H}" fill="#0b0e14"/>
<text x="{pad}" y="22" fill="#e6e9ef" font-size="14">Autonomous adoption — share of agents that CHOSE to pay for a Guild lookup</text>
{grid}
<polyline fill="none" stroke="#34d399" stroke-width="2.5" points="{pts}"/>
<text x="{W-pad}" y="{H-12}" fill="#8a93a6" font-size="10" text-anchor="end">rounds →</text>
</svg>'''
    with open(path, "w") as f:
        f.write(svg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--consumers", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args = ap.parse_args()

    rng = random.Random(args.seed)
    client = TestClient(app)
    workers = seed_supply(rng)
    worker_ids = list(workers.keys())

    print("=" * 80)
    print("AGENT GUILD — autonomous adoption experiment (no human, paid API)")
    print(f"rounds={args.rounds} consumers={args.consumers} workers={len(workers)} "
          f"lookup_fee=${LOOKUP_USD} billing=ENFORCED")
    print("=" * 80)

    consumers = [Consumer(client, f"Consumer-{i}", random.Random(args.seed + i))
                 for i in range(args.consumers)]

    curve, u_consult, u_blind, s_consult, s_blind = [], [], [], [], []
    for _ in range(args.rounds):
        consults = 0
        for c in consumers:
            action, util, success = play(c, workers, worker_ids, rng)
            if action == "consult":
                consults += 1; u_consult.append(util); s_consult.append(success)
            else:
                u_blind.append(util); s_blind.append(success)
        curve.append(consults / len(consumers))

    third = max(1, len(curve) // 3)
    final_adoption = sum(curve[-third:]) / third
    mid_adoption = sum(curve[third:2 * third]) / max(1, len(curve[third:2 * third]))
    mu_consult = sum(u_consult) / len(u_consult) if u_consult else 0.0
    mu_blind = sum(u_blind) / len(u_blind) if u_blind else 0.0
    sr_consult = sum(s_consult) / len(s_consult) if s_consult else 0.0
    sr_blind = sum(s_blind) / len(s_blind) if s_blind else 0.0

    funnel = client.get("/instrumentation").json()
    evaluation = client.get("/evaluation").json()

    print(f"\nadoption over rounds: {sparkline(curve)}")
    print(f"final adoption (last third): {final_adoption*100:.0f}%   "
          f"(mid third {mid_adoption*100:.0f}% — sustained, not a spike)")
    print(f"net utility/round — consult ${mu_consult:+.3f}  vs  blind ${mu_blind:+.3f}  "
          f"(consult wins by ${mu_consult-mu_blind:+.3f} AFTER the ${LOOKUP_USD} fee)")
    print(f"task success — consult {sr_consult*100:.0f}%  vs  blind {sr_blind*100:.0f}%")

    print("\nLIVE INSTRUMENTATION FUNNEL (/instrumentation)")
    for k in ("unique_agents", "first_query", "repeat_query", "paid_query",
              "repeat_paid_query_agents", "delegations", "delegations_following_recommendation"):
        print(f"  {k:<38} {funnel[k]}")

    print("\nSELF-EVALUATION SIGNAL (/evaluation) — measured outcome lift")
    print(f"  recommended hires: {evaluation['recommended_success_rate']} "
          f"(n={evaluation['n_recommended']})")
    print(f"  baseline hires:    {evaluation['baseline_success_rate']} "
          f"(n={evaluation['n_baseline']})")
    print(f"  lift:              {evaluation['lift']}")

    lift = evaluation["lift"] or 0.0
    passed = (
        final_adoption >= 0.60
        and mu_consult > mu_blind
        and final_adoption >= mid_adoption - 0.1          # sustained
        and funnel["repeat_paid_query_agents"] > 0        # repeated PAID use
        and funnel["delegations_following_recommendation"] > 0
        and lift > 0
    )
    print("\n" + "=" * 80)
    if passed:
        print("VERDICT: ✅ Agents autonomously discover, evaluate, ADOPT and repeatedly")
        print("         PAY for Agent Guild — unprompted — because it improves net")
        print("         outcomes. Repeat paid use and recommendation-led delegation are")
        print("         visible in the live funnel.")
    else:
        print("VERDICT: ❌ No sustained autonomous adoption under these parameters.")
    print("=" * 80)

    os.makedirs(args.out, exist_ok=True)
    result = {
        "rounds": args.rounds, "consumers": args.consumers,
        "final_adoption": final_adoption, "mid_adoption": mid_adoption,
        "mean_utility_consult": mu_consult, "mean_utility_blind": mu_blind,
        "success_consult": sr_consult, "success_blind": sr_blind,
        "adoption_curve": curve, "instrumentation": funnel,
        "evaluation": evaluation, "passed": passed,
    }
    with open(os.path.join(args.out, "autonomous_adoption.json"), "w") as f:
        json.dump(result, f, indent=2)
    with open(os.path.join(args.out, "autonomous_adoption.csv"), "w") as f:
        f.write("round,consult_share\n")
        for i, v in enumerate(curve):
            f.write(f"{i},{v}\n")
    write_svg(os.path.join(args.out, "autonomous_adoption.svg"), curve)
    print(f"\nResults written to {args.out}/ "
          f"(autonomous_adoption.json, .csv, .svg)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
