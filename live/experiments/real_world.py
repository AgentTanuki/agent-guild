"""Real-world validation — convergence experiment with real LLM-backed workers.

Three conditions, deterministic ground-truth evaluation, an autonomous hiring
agent free to use or bypass the Guild, real OpenAI + Anthropic workers when keys
are present (offline deterministic backend otherwise, clearly labeled).

    A  random selection, no Guild
    B  Guild-only selection
    C  free choice (agent may use Guild or bypass it; it learns)

Usage:
    python experiments/real_world.py --estimate-only          # cost estimate, no calls
    python experiments/real_world.py                          # offline self-validation
    OPENAI_API_KEY=... ANTHROPIC_API_KEY=... \
        python experiments/real_world.py --provider auto --yes   # real run

Outputs (in experiments/results/): real_world_results.json, real_world_transactions.csv,
real_world_adoption.csv, and REAL_WORLD_FINDINGS.md (auto-generated from the run).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass, asdict

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LIVE, "agentkit"))
sys.path.insert(0, os.path.join(LIVE, "agents"))
sys.path.insert(0, os.path.join(LIVE, "guild"))
os.environ.setdefault("GUILD_DATA", "")

from agentguild import GuildClient, llm                       # noqa: E402
from worker_agent import WorkerProfile, WorkerAgent, CAPABILITY  # noqa: E402
from hiring_agent import HiringAgent, SelectionWeights, Outcome  # noqa: E402
import tasks_real as T                                        # noqa: E402


# --------------------------------------------------------------------------- #
# Economics + pricing
# --------------------------------------------------------------------------- #
@dataclass
class Econ:
    reward: float = 1.0
    penalty: float = 1.5
    latency_cost: float = 0.05
    guild_query_cost: float = 0.02


def utility(o: Outcome, e: Econ) -> float:
    u = (e.reward if o.correct else -e.penalty) - o.price - e.latency_cost * (o.latency_ms / 1000.0)
    if o.used_guild:
        u -= e.guild_query_cost
    return u


# Approx USD per 1M tokens (input, output). Used for the cost ESTIMATE only.
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "claude-3-5-sonnet-latest": (3.00, 15.0),
    "mock-1": (0.0, 0.0),
}
INPUT_TOK, OUTPUT_TOK = 220, 60


def call_cost_usd(model: str) -> float:
    pin, pout = PRICES.get(model, (0.3, 1.2))
    return INPUT_TOK * pin / 1e6 + OUTPUT_TOK * pout / 1e6


# --------------------------------------------------------------------------- #
# Worker pool — two model families, varied tiers, a specialist
# --------------------------------------------------------------------------- #
def build_pool(real_mode: bool, available: list[str]) -> list[WorkerProfile]:
    # Declared (provider, model) per role; remapped if that provider is absent.
    spec = [
        # name, provider, model, reliability, specialty, price, latency
        ("Atlas-Premium", "openai", "gpt-4o-mini", "high", "general", 0.020, 1100),
        ("Haiku-Reliable", "anthropic", "claude-3-5-haiku-latest", "high", "general", 0.014, 850),
        ("Mini-Fast", "openai", "gpt-4o-mini", "medium", "general", 0.006, 450),
        ("Budget-Guess", "openai", "gpt-4o-mini", "low", "general", 0.003, 300),
        ("Sci-Specialist", "anthropic", "claude-3-5-haiku-latest", "high", "science", 0.011, 700),
        ("Sloppy-Claude", "anthropic", "claude-3-5-haiku-latest", "low", "general", 0.008, 500),
    ]
    real_providers = [p for p in ("openai", "anthropic") if p in available]

    def remap(provider: str, model: str) -> tuple[str, str]:
        if not real_mode:
            return "mock", "mock-1"
        if provider in real_providers:
            return provider, model
        # provider key missing → use the first available real provider with a sensible model
        alt = real_providers[0]
        alt_model = "gpt-4o-mini" if alt == "openai" else "claude-3-5-haiku-latest"
        return alt, alt_model

    pool = []
    for name, prov, model, rel, sp, price, lat in spec:
        p, m = remap(prov, model)
        pool.append(WorkerProfile(name, p, m, rel, sp, price, lat))
    return pool


# --------------------------------------------------------------------------- #
# In-process Guild
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def launch_guild():
    import uvicorn
    import app.main as gm
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(gm.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}", gm.store


def reset(store):
    store.agents.clear(); store.attestations.clear(); store._rep_cache = None


def register_pool(client: GuildClient, pool: list[WorkerProfile]) -> tuple[dict, list[str], dict]:
    workers, ids, profiles = {}, [], {}
    for pr in pool:
        ident = client.register(pr.name, [CAPABILITY], metadata=pr.metadata())
        workers[ident.id] = WorkerAgent(pr)
        profiles[ident.id] = pr
        ids.append(ident.id)
    return workers, ids, profiles


# --------------------------------------------------------------------------- #
# Bandit (free-choice learner) — recency-weighted, non-stationary aware
# --------------------------------------------------------------------------- #
@dataclass
class Bandit:
    q_guild: float = 0.6
    q_random: float = 0.6
    alpha: float = 0.3
    eps0: float = 0.3
    t: int = 0
    tg: bool = False
    tr: bool = False

    def eps(self):
        return max(0.04, self.eps0 * (0.93 ** self.t))

    def choose(self, rng):
        if not self.tg:
            return True
        if not self.tr:
            return False
        if rng.random() < self.eps():
            return rng.random() < 0.5
        return self.q_guild >= self.q_random

    def update(self, used_guild, u):
        self.t += 1
        if used_guild:
            self.tg = True; self.q_guild += self.alpha * (u - self.q_guild)
        else:
            self.tr = True; self.q_random += self.alpha * (u - self.q_random)


# --------------------------------------------------------------------------- #
# Conditions
# --------------------------------------------------------------------------- #
def agg(rows, econ):
    n = len(rows)
    if n == 0:
        return {}
    correct = sum(r.correct for r in rows)
    return {
        "transactions": n,
        "success_rate": correct / n,
        "failure_rate": 1 - correct / n,
        "avg_utility": sum(utility(r, econ) for r in rows) / n,
        "avg_price": sum(r.price for r in rows) / n,
        "avg_latency_ms": sum(r.latency_ms for r in rows) / n,
        "real_cost_usd": sum(call_cost_usd(r.model) for r in rows),
    }


def run_A(client, store, pool, econ, n, offline, seed):
    reset(store)
    rng = random.Random(seed)
    workers, ids, _ = register_pool(client, pool)
    me = client.register("Hirer-A", ["research"], seed=True)
    agent = HiringAgent(client, me, workers, rng, offline=offline)
    rows = []
    for i in range(n):
        task = T.TASKS[i % len(T.TASKS)]
        # Pure random selection, no Guild query, no attestation recorded.
        rows.append(agent.run_task(task, use_guild=False, known_ids=ids))
    return rows


def run_B(client, store, pool, econ, n, offline, seed):
    reset(store)
    rng = random.Random(seed)
    workers, ids, _ = register_pool(client, pool)
    me = client.register("Hirer-B", ["research"], seed=True)
    agent = HiringAgent(client, me, workers, rng, offline=offline)
    rows = []
    for i in range(n):
        task = T.TASKS[i % len(T.TASKS)]
        rows.append(agent.run_task(task, use_guild=True, known_ids=ids))
    return rows


def run_C(client, store, pool, econ, rounds, consumers, offline, seed):
    reset(store)
    rng = random.Random(seed)
    workers, ids, _ = register_pool(client, pool)
    agents = [HiringAgent(client, client.register(f"Hirer-C{i}", ["research"], seed=True),
                          workers, rng, offline=offline) for i in range(consumers)]
    bandits = [Bandit() for _ in range(consumers)]
    rows, adoption = [], []
    guild_rows_in_order = []
    ti = 0
    for r in range(rounds):
        used = 0
        for agent, b in zip(agents, bandits):
            task = T.TASKS[ti % len(T.TASKS)]; ti += 1
            ug = b.choose(rng)
            o = agent.run_task(task, use_guild=ug, known_ids=ids)
            u = utility(o, econ)
            b.update(ug, u)
            rows.append(o)
            if ug:
                guild_rows_in_order.append(o)
            used += 1 if ug else 0
        adoption.append(used / consumers)
    # Does the graph improve selection? success of Guild-picks early vs late.
    g = guild_rows_in_order
    third = max(1, len(g) // 3)
    early = sum(o.correct for o in g[:third]) / third if g else 0.0
    late = sum(o.correct for o in g[-third:]) / third if g else 0.0
    tail = adoption[max(0, len(adoption) * 2 // 3):]
    ug_rows = [o for o in rows if o.used_guild]
    rr_rows = [o for o in rows if not o.used_guild]
    return {
        "rows": rows,
        "adoption_curve": adoption,
        "final_adoption": sum(tail) / len(tail) if tail else 0.0,
        "mean_utility_guild": (sum(utility(o, econ) for o in ug_rows) / len(ug_rows)) if ug_rows else 0.0,
        "mean_utility_random": (sum(utility(o, econ) for o in rr_rows) / len(rr_rows)) if rr_rows else 0.0,
        "guild_select_success_early": early,
        "guild_select_success_late": late,
        "n_guild": len(ug_rows),
        "n_random": len(rr_rows),
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def sparkline(values):
    bars = "▁▂▃▄▅▆▇█"
    return "".join(bars[min(7, int(v * 7))] for v in values)


def estimate(pool, nA, nB, rounds, consumers, real_mode):
    total_calls = nA + nB + rounds * consumers
    if not real_mode:
        return total_calls, 0.0, 0.0
    avg = sum(call_cost_usd(p.model) for p in pool) / len(pool)
    mx = max(call_cost_usd(p.model) for p in pool)
    return total_calls, total_calls * avg, total_calls * mx


def recommendation(real_mode, A, B, C):
    if not real_mode:
        return ("VALIDATION-ONLY",
                "Offline self-validation only — no real LLM calls were made. Run with a provider "
                "key and --yes to obtain a real-world result before deciding.")
    quality_gain = B["success_rate"] - A["success_rate"]
    adoption = C["final_adoption"]
    util_gain = C["mean_utility_guild"] - C["mean_utility_random"]
    if quality_gain <= 0.0:
        return ("STOP", f"The Guild did NOT improve task success (B−A = {quality_gain:+.1%}). "
                        "Reputation-based selection is not beating random here. Stop and rethink the "
                        "signal before building further.")
    if adoption >= 0.6 and util_gain > 0.02:
        return ("CONTINUE", f"The Guild improved success by {quality_gain:+.1%} and free agents "
                            f"converged to {adoption:.0%} adoption with higher utility "
                            f"({util_gain:+.3f}). Evidence of voluntary adoption survives real model "
                            "behaviour. Continue — widen tasks, models, and scale.")
    return ("REDESIGN", f"The Guild improved success by {quality_gain:+.1%} but free agents did NOT "
                        f"converge (adoption {adoption:.0%}, utility gain {util_gain:+.3f}). The signal "
                        "exists but is too weak/slow to drive adoption. Redesign incentives or learning "
                        "before scaling.")


def write_findings(path, meta, A, B, C, rec):
    label, text = rec
    real = meta["real_mode"]
    banner = ("REAL LLM RUN — " + ", ".join(meta["models"])) if real else \
        "⚠ OFFLINE SELF-VALIDATION (deterministic backend, NOT a real-world result)"
    lines = [
        "# Agent Guild — Real-World Findings",
        "",
        f"> **{banner}**",
        f"> provider mode: `{meta['provider_mode']}` · transactions: {meta['total_tx']} · "
        f"estimated spend: ${meta['est_cost']:.4f} · seed: {meta['seed']}",
        "",
        "## Question",
        "",
        "Do autonomous agents choose Agent Guild **because it improves their own expected "
        "utility** — when they are free to bypass it? Not \"can we make them\", but \"do they\".",
        "",
        "## Conditions (deterministic ground-truth evaluation)",
        "",
        "| Condition | Success | Failure | Avg utility | Avg latency | Real $ |",
        "|-----------|--------:|--------:|------------:|------------:|-------:|",
        f"| A — random, no Guild | {A['success_rate']:.1%} | {A['failure_rate']:.1%} | "
        f"{A['avg_utility']:.3f} | {A['avg_latency_ms']:.0f} ms | ${A['real_cost_usd']:.4f} |",
        f"| B — Guild-only | {B['success_rate']:.1%} | {B['failure_rate']:.1%} | "
        f"{B['avg_utility']:.3f} | {B['avg_latency_ms']:.0f} ms | ${B['real_cost_usd']:.4f} |",
        "",
        f"**Quality gain from the Guild (B − A): {B['success_rate'] - A['success_rate']:+.1%}.**",
        "",
        "## C — free choice (agents may bypass the Guild)",
        "",
        f"- adoption over rounds: `{sparkline(C['adoption_curve'])}`",
        f"- **final adoption (last third): {C['final_adoption']:.0%}**",
        f"- mean utility — Guild action {C['mean_utility_guild']:.3f} vs random "
        f"{C['mean_utility_random']:.3f} (gain {C['mean_utility_guild'] - C['mean_utility_random']:+.3f})",
        f"- does the attestation graph improve selection? Guild-pick success "
        f"{C['guild_select_success_early']:.0%} (early) → {C['guild_select_success_late']:.0%} (late)",
        "",
        "## Honest caveats",
        "",
        f"- **Sample size is small** ({meta['total_tx']} transactions). Treat all numbers as "
        "directional, not precise; re-run with more rounds and a different `--seed` to gauge noise.",
        "- The **\"premium\" worker is not a bigger model** in the cheapest setup — its quality edge "
        "comes from a careful prompt, so real quality/price spread may be understated. Use "
        "`--premium` mental model (swap gpt-4o / sonnet) for a harsher test.",
        "- The hiring agent's selection, the learner, and the economic weights are **parameters**; "
        "the result is conditional on them. They are in source and on the CLI.",
        "- Summary tasks use a **keyword rubric**, the least clean of the four evaluators.",
    ]
    if not real:
        lines += ["- **These specific numbers are from the offline backend** and exist only to prove "
                  "the harness runs end to end. They are NOT evidence about real model behaviour."]
    lines += [
        "",
        "## Recommendation",
        "",
        f"**{label}.** {text}",
        "",
        "## Reproduce",
        "",
        "```bash",
        "cd live",
        "pip install -r guild/requirements.txt openai anthropic",
        "export OPENAI_API_KEY=...   # and/or ANTHROPIC_API_KEY",
        "python experiments/real_world.py --estimate-only      # see cost first",
        "python experiments/real_world.py --provider auto --yes",
        "```",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="auto", choices=["auto", "openai", "anthropic", "mock"])
    ap.add_argument("--n", type=int, default=40, help="transactions for conditions A and B each")
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--consumers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--yes", action="store_true", help="confirm spending on a real run")
    ap.add_argument("--estimate-only", action="store_true")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args = ap.parse_args()

    available = llm.available_providers()
    if args.provider == "mock":
        real_mode = False
    elif args.provider == "auto":
        real_mode = any(p in available for p in ("openai", "anthropic"))
    else:
        real_mode = args.provider in available
        if not real_mode:
            print(f"Provider '{args.provider}' unavailable (key/SDK missing). Available: {available}")
            return

    pool = build_pool(real_mode, available)
    econ = Econ()
    total_calls, est_avg, est_max = estimate(pool, args.n, args.n, args.rounds, args.consumers, real_mode)

    print("=" * 78)
    print("AGENT GUILD — real-world validation")
    print(f"mode: {'REAL LLM' if real_mode else 'OFFLINE (no key)'} | providers available: {available}")
    print(f"workers: " + ", ".join(f"{p.name}[{p.provider}/{p.model},{p.reliability}]" for p in pool))
    print(f"plan: A={args.n} B={args.n} C={args.rounds}x{args.consumers}  → {total_calls} LLM calls")
    print(f"COST ESTIMATE: ${est_avg:.4f} expected  (worst-case ${est_max:.4f})"
          if real_mode else "COST ESTIMATE: $0.0000 (offline)")
    print("=" * 78)

    if args.estimate_only:
        # Show the projected REAL spend (assuming both providers) even if no key is set yet.
        rpool = build_pool(True, ["openai", "anthropic"])
        tc, ea, em = estimate(rpool, args.n, args.n, args.rounds, args.consumers, True)
        print(f"\nProjected REAL-run cost (gpt-4o-mini + claude-3-5-haiku, "
              f"{INPUT_TOK}+{OUTPUT_TOK} tokens/call):")
        print(f"  {tc} calls → ${ea:.4f} expected, ${em:.4f} worst-case.")
        print("estimate-only: no calls made.")
        return
    if real_mode and not args.yes:
        print("Real run will spend money. Re-run with --yes to proceed.")
        return

    server, base_url, store = launch_guild()
    client = GuildClient(base_url)
    offline = not real_mode
    try:
        rows_A = run_A(client, store, pool, econ, args.n, offline, args.seed)
        rows_B = run_B(client, store, pool, econ, args.n, offline, args.seed)
        A, B = agg(rows_A, econ), agg(rows_B, econ)
        C = run_C(client, store, pool, econ, args.rounds, args.consumers, offline, args.seed)
        Cm = {k: v for k, v in C.items() if k != "rows"}

        print(f"\nA random : success {A['success_rate']:.1%}  util {A['avg_utility']:.3f}")
        print(f"B guild  : success {B['success_rate']:.1%}  util {B['avg_utility']:.3f}  "
              f"(quality gain {B['success_rate']-A['success_rate']:+.1%})")
        print(f"C free   : adoption {sparkline(C['adoption_curve'])}  final {C['final_adoption']:.0%}  "
              f"util g/r {C['mean_utility_guild']:.3f}/{C['mean_utility_random']:.3f}")
        print(f"          graph-improves-selection: {C['guild_select_success_early']:.0%} → "
              f"{C['guild_select_success_late']:.0%}")

        rec = recommendation(real_mode, A, B, Cm)
        print(f"\nRECOMMENDATION: {rec[0]} — {rec[1]}")

        os.makedirs(args.out, exist_ok=True)
        meta = {
            "real_mode": real_mode, "provider_mode": args.provider,
            "models": sorted(set(p.model for p in pool)),
            "total_tx": len(rows_A) + len(rows_B) + len(C["rows"]),
            "est_cost": est_avg, "seed": args.seed,
        }
        with open(os.path.join(args.out, "real_world_results.json"), "w") as f:
            json.dump({"meta": meta, "econ": asdict(econ), "A": A, "B": B, "C": Cm,
                       "recommendation": {"label": rec[0], "text": rec[1]}}, f, indent=2)
        with open(os.path.join(args.out, "real_world_adoption.csv"), "w") as f:
            f.write("round,guild_share\n")
            for i, v in enumerate(C["adoption_curve"]):
                f.write(f"{i},{v}\n")
        with open(os.path.join(args.out, "real_world_transactions.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["condition", "used_guild", "worker", "provider", "model",
                        "task_type", "correct", "price", "latency_ms"])
            for cond, rows in (("A", rows_A), ("B", rows_B), ("C", C["rows"])):
                for o in rows:
                    w.writerow([cond, o.used_guild, o.chosen_name, o.provider, o.model,
                                o.task_type, o.correct, o.price, round(o.latency_ms, 1)])
        write_findings(os.path.join(LIVE, "REAL_WORLD_FINDINGS.md"), meta, A, B, Cm, rec)
        print(f"\nWrote results to {args.out}/ and REAL_WORLD_FINDINGS.md")
    finally:
        server.should_exit = True
        time.sleep(0.2)


if __name__ == "__main__":
    main()
