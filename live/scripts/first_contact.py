#!/usr/bin/env python3
"""Agent Guild — first-contact live test (stdlib only, double-click runnable).

Seeds lightweight supply if the graph is empty, then runs the full autonomous
loop against the live endpoint and prints every step: register -> trial credits
-> balance -> manifest -> paid best-agent lookup -> paid risk-score -> charge
headers -> instrumentation before/after.
"""
import json, os, sys, urllib.request, urllib.error, hashlib, random

GUILD = os.environ.get("GUILD_URL", "https://agent-guild-5d5r.onrender.com").rstrip("/")
CAP = os.environ.get("CAP", "fact-check")


def req(method, path, key=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(GUILD + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    # mark this as our own test traffic so it stays out of the "external" signal
    r.add_header("X-Guild-Source", "first-party")
    if key:
        r.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(r, timeout=45) as resp:
            # keep the HTTPMessage (case-insensitive .get) rather than a dict
            return resp.status, resp.headers, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, json.loads(e.read().decode() or "{}")


def reg(name, caps, meta=None):
    _, _, d = req("POST", "/agents/register",
                  body={"name": name, "capabilities": caps, "metadata": meta or {}})
    return d


def seed_if_needed():
    _, _, s = req("GET", f"/search?capability={CAP}&limit=5")
    if s.get("count", 0) >= 3:
        print(f"    supply already present ({s['count']} {CAP} agents) — skipping seed")
        return
    print("    seeding lightweight supply (no admin token needed)…")
    rng = random.Random(1)
    reviewers = [reg(f"Seed-Reviewer-{i}", ["hiring"], {"seed_supply": True}) for i in range(2)]
    roster = [("Ace", 0.95, 0.03), ("Pro", 0.86, 0.02), ("Solid", 0.74, 0.014),
              ("Meh", 0.55, 0.009), ("Weak", 0.34, 0.006)]
    for nm, q, price in roster:
        w = reg(nm, [CAP], {"seed_supply": True, "price_per_call": price})
        for rv in reviewers:
            for _ in range(2):
                _, _, t = req("POST", "/tasks", key=rv["api_key"],
                              body={"requester_id": rv["id"], "worker_id": w["id"],
                                    "task_type": CAP, "payment": price})
                h = "0x" + hashlib.sha256((w["id"] + t["id"]).encode()).hexdigest()[:16]
                req("POST", f"/tasks/{t['id']}/receipt", key=w["api_key"],
                    body={"deliverable_hash": h, "outcome": "delivered"})
                rating = max(0.0, min(1.0, q + rng.uniform(-0.04, 0.04)))
                req("POST", "/attestations", key=rv["api_key"],
                    body={"issuer_id": rv["id"], "subject_id": w["id"],
                          "capability": CAP, "rating": rating, "task_id": t["id"]})
    print("    seeded 5 fact-check workers with receipt-backed attestations")


def main():
    print(f"Agent Guild — first contact  →  {GUILD}")
    print("=" * 66)

    _, _, before = req("GET", "/instrumentation")
    print("\n[0] Instrumentation BEFORE:", json.dumps(before))

    print("\n[seed] checking supply…")
    seed_if_needed()

    print("\n[1] Register a trial consumer agent (POST /agents/register):")
    agent = reg("FirstContact-Consumer", ["research"], {"external": True})
    print("    agent id =", agent.get("id"))

    print("\n[2] Claim trial credits, no human (POST /billing/trial):")
    _, _, trial = req("POST", "/billing/trial")
    key = trial["key"]
    print(f"    key = {key[:14]}…   balance = {trial['balance']} credits")

    print("\n[3] Confirm balance (GET /billing/account):")
    _, _, acct = req("GET", "/billing/account", key=key)
    print(f"    balance = {acct['balance']}   credit_usd = ${acct['credit_usd']}")

    print("\n[4] Read the discovery manifest:")
    _, _, m = req("GET", "/.well-known/agent-guild.json")
    print(f"    {m['name']} v{m['version']}  | discover="
          f"{m['endpoints']['discover']['cost_credits']}cr "
          f"risk_score={m['endpoints']['risk_score']['cost_credits']}cr")

    print("\n[5] Best-agent lookup — PAID (GET /search):")
    st, hdr, body = req("GET", f"/search?capability={CAP}&limit=1", key=key)
    cost, bal = hdr.get("X-Guild-Cost"), hdr.get("X-Guild-Balance")
    if body.get("count", 0) == 0:
        print("    no supply returned — seeding may have failed; re-run.")
        print(f"    (lookup was still metered: X-Guild-Cost={cost} X-Guild-Balance={bal})")
        return
    best = body["results"][0]
    print(f"    recommended: {best['name']}  (trust={round(best['trust'],1)}, id={best['id']})")

    print("\n[6] Risk-score on the recommended agent — PAID:")
    _, _, risk = req("GET", f"/agents/{best['id']}/risk-score", key=key)
    print(f"    risk={risk['risk']} → {risk['recommendation']}  "
          f"(confidence={risk['confidence']}, collusion_suspicion={risk['collusion_suspicion']})")

    print("\n[7] Charge headers from the lookup (proof credits were spent):")
    print(f"    X-Guild-Cost: {cost}    X-Guild-Balance: {bal}")

    print("\n[8] Balance after spending (GET /billing/account):")
    _, _, acct2 = req("GET", "/billing/account", key=key)
    print(f"    balance = {acct2['balance']}   spent = {acct2['spent']} credits")

    print("\n[9] Instrumentation AFTER:")
    _, _, after = req("GET", "/instrumentation")
    print("   ", json.dumps(after))

    print("\n" + "=" * 66)
    print("✅ Full live loop: discover → self-fund → paid lookup → risk-check.")
    print(f"   funnel moved: paid_query {before.get('paid_query')}→{after.get('paid_query')}, "
          f"unique_agents {before.get('unique_agents')}→{after.get('unique_agents')}, "
          f"delegations {before.get('delegations')}→{after.get('delegations')}")


if __name__ == "__main__":
    main()
