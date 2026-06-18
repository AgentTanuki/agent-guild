"""Phase 3 — the rational-agent test.

Question (per the brief): do autonomous agents *voluntarily converge* on Agent
Guild as a trust mechanism when they are free to ignore it?

We run real agents over the real Guild API with genuine economic trade-offs
(quality, price, latency, plus the cost of consulting the Guild). Two parts:

  PART A  Comparative baseline — Env A (no Guild, random selection) vs
          Env B (Guild available, reputation-based selection). 100+ real
          transactions each. Measures success / failure / cost / quality.

  PART B  Free-choice convergence — a population of consumer agents, each free
          every round to consult the Guild (small query cost) or pick at random
          (free but blind). They learn from realized utility (epsilon-greedy).
          We observe whether adoption of the Guild emerges endogenously as the
          attestation graph fills in. THIS is the real test.

Task execution uses real LLM calls when a provider key is available; otherwise a
deterministic offline backend so the harness is self-testable. Select with
--provider {auto,openai,anthropic,mock}.

Run:
    python experiments/phase3.py                 # auto provider, defaults
    python experiments/phase3.py --rounds 25 --consumers 8 --provider openai
"""
from __future__ import annotations

import argparse
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

os.environ.setdefault("GUILD_DATA", "")  # in-memory store

from agentguild import GuildClient, llm                      # noqa: E402
from workers import WorkerProfile                            # noqa: E402
from runtime import AgentRuntime                             # noqa: E402
from research_agent import ResearchAgent, SelectionWeights   # noqa: E402
from claims import CLAIMS                                    # noqa: E402


# --------------------------------------------------------------------------- #
# Economic model
# --------------------------------------------------------------------------- #
@dataclass
class Econ:
    reward: float = 1.0            # utility for a correct fact-check
    penalty: float = 1.5          # a WRONG fact-check is costlier than a right one is
    #                               valuable: acting on false info corrupts downstream work
    latency_cost: float = 0.05    # utility cost per second of latency
    guild_query_cost: float = 0.02  # cost of consulting the Guild (verification)


def utility(correct: bool, price: float, latency_ms: float, used_guild: bool, e: Econ) -> float:
    u = (e.reward if correct else -e.penalty)
    u -= price
    u -= e.latency_cost * (latency_ms / 1000.0)
    if used_guild:
        u -= e.guild_query_cost
    return u


# --------------------------------------------------------------------------- #
# Worker pool — real spread of quality / price / latency
# --------------------------------------------------------------------------- #
def worker_pool(provider: str) -> list[WorkerProfile]:
    # For real providers we keep one cheap model and vary *reliability* (prompt +
    # behaviour) to create genuine accuracy differences without large cost.
    model = {
        "openai": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "anthropic": os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"),
        "mock": "mock-1",
    }.get(provider, "mock-1")
    P = lambda name, rel, price, lat: WorkerProfile(  # noqa: E731
        name=name, capability="fact-check", provider=provider, model=model,
        reliability=rel, price_per_call=price, est_latency_ms=lat,
    )
    return [
        P("Atlas-Pro", "high", 0.050, 1500),     # great but pricey & slow
        P("Sage-Reliable", "high", 0.030, 900),  # great value (the rational pick)
        P("Mini-Decent", "medium", 0.012, 500),  # ok, cheap
        P("Sloppy-Mid", "low", 0.020, 700),       # bad value: pricey AND unreliable
        P("QuickGuess", "low", 0.004, 300),       # cheap trap: fast & wrong
        P("Random-Bot", "low", 0.006, 250),       # cheap trap
    ]


# --------------------------------------------------------------------------- #
# In-process server
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def launch_guild():
    import uvicorn
    import app.main as guild_main  # the FastAPI app + its store
    port = _free_port()
    config = uvicorn.Config(guild_main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}", guild_main.store


def reset_store(store):
    store.agents.clear()
    store.attestations.clear()
    store._rep_cache = None


# --------------------------------------------------------------------------- #
# World setup
# --------------------------------------------------------------------------- #
def setup_workers(client: GuildClient, provider: str) -> tuple[AgentRuntime, list[str]]:
    runtime = AgentRuntime(client)
    ids = [runtime.register_factchecker(p) for p in worker_pool(provider)]
    return runtime, ids


def new_consumer(client: GuildClient, runtime: AgentRuntime, rng: random.Random, name: str) -> ResearchAgent:
    ident = client.register(name=name, capabilities=["research"], seed=True)  # verified hirer = seed
    return ResearchAgent(client, ident, runtime, rng, SelectionWeights())


# --------------------------------------------------------------------------- #
# PART A — comparative baseline
# --------------------------------------------------------------------------- #
def run_env(client, store, provider, n, use_guild, econ, seed) -> dict:
    reset_store(store)
    rng = random.Random(seed)
    runtime, fc_ids = setup_workers(client, provider)
    consumer = new_consumer(client, runtime, rng, "ResearchAgent-A" if not use_guild else "ResearchAgent-B")
    rows = []
    for i in range(n):
        claim, label = CLAIMS[i % len(CLAIMS)]
        out = consumer.fact_check(claim, label, use_guild=use_guild, known_factcheckers=fc_ids)
        u = utility(out.correct, out.price, out.latency_ms, out.used_guild, econ)
        rows.append((out.correct, out.price, out.latency_ms, u, out.chosen_name))
    n = len(rows)
    correct = sum(1 for r in rows if r[0])
    return {
        "env": "B (Guild)" if use_guild else "A (random)",
        "transactions": n,
        "success_rate": correct / n,
        "failure_rate": 1 - correct / n,
        "avg_price": sum(r[1] for r in rows) / n,
        "avg_latency_ms": sum(r[2] for r in rows) / n,
        "verification_cost": (econ.guild_query_cost * n) if use_guild else 0.0,
        "avg_utility": sum(r[3] for r in rows) / n,
        "total_utility": sum(r[3] for r in rows),
    }


# --------------------------------------------------------------------------- #
# PART B — free-choice convergence (the real test)
# --------------------------------------------------------------------------- #
@dataclass
class Bandit:
    """Each consumer's own learner over two meta-actions: {use Guild, go random}.

    Uses recency-weighted (constant-α) value estimates rather than sample
    averages, because the environment is NON-STATIONARY: the Guild is useless on
    round 1 (empty graph) and becomes informative as attestations accumulate. A
    sample-average learner that samples the Guild early would lock onto 'random';
    a recency-weighted learner re-evaluates the Guild as it improves — which is
    exactly the rational response, and what lets voluntary adoption emerge."""
    q_guild: float = 0.6      # optimistic init encourages trying both
    q_random: float = 0.6
    alpha: float = 0.3        # recency weight (tracks the non-stationary Guild)
    epsilon0: float = 0.3
    t: int = 0
    tried_guild: bool = False
    tried_random: bool = False

    def epsilon(self) -> float:
        return max(0.04, self.epsilon0 * (0.93 ** self.t))

    def choose(self, rng: random.Random) -> bool:
        if not self.tried_guild:
            return True
        if not self.tried_random:
            return False
        if rng.random() < self.epsilon():
            return rng.random() < 0.5
        return self.q_guild >= self.q_random

    def update(self, used_guild: bool, u: float) -> None:
        self.t += 1
        if used_guild:
            self.tried_guild = True
            self.q_guild += self.alpha * (u - self.q_guild)
        else:
            self.tried_random = True
            self.q_random += self.alpha * (u - self.q_random)


def run_free_choice(client, store, provider, rounds, consumers_n, econ, seed) -> dict:
    reset_store(store)
    rng = random.Random(seed)
    runtime, fc_ids = setup_workers(client, provider)
    consumers = [new_consumer(client, runtime, rng, f"Consumer-{i}") for i in range(consumers_n)]
    bandits = [Bandit() for _ in range(consumers_n)]

    adoption_curve = []     # guild-share per round
    util_guild, util_random = [], []
    transactions = 0
    ci = 0
    for r in range(rounds):
        used_guild_count = 0
        for c, (consumer, bandit) in enumerate(zip(consumers, bandits)):
            claim, label = CLAIMS[ci % len(CLAIMS)]
            ci += 1
            use_guild = bandit.choose(rng)
            out = consumer.fact_check(claim, label, use_guild=use_guild, known_factcheckers=fc_ids)
            u = utility(out.correct, out.price, out.latency_ms, out.used_guild, econ)
            bandit.update(use_guild, u)
            (util_guild if use_guild else util_random).append(u)
            used_guild_count += 1 if use_guild else 0
            transactions += 1
        adoption_curve.append(used_guild_count / consumers_n)

    # Final adoption = share over the last third of rounds (post-learning).
    tail = adoption_curve[max(0, len(adoption_curve) * 2 // 3):]
    final_adoption = sum(tail) / len(tail)
    mean_u_guild = sum(util_guild) / len(util_guild) if util_guild else 0.0
    mean_u_random = sum(util_random) / len(util_random) if util_random else 0.0
    return {
        "transactions": transactions,
        "rounds": rounds,
        "consumers": consumers_n,
        "adoption_curve": adoption_curve,
        "final_adoption": final_adoption,
        "mean_utility_guild": mean_u_guild,
        "mean_utility_random": mean_u_random,
        "utility_gain": mean_u_guild - mean_u_random,
        "n_guild_actions": len(util_guild),
        "n_random_actions": len(util_random),
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def sparkline(values: list[float]) -> str:
    bars = "▁▂▃▄▅▆▇█"
    return "".join(bars[min(len(bars) - 1, int(v * (len(bars) - 1)))] for v in values)


def write_svg(path: str, curve: list[float], a: dict, b: dict) -> None:
    W, H, pad = 720, 280, 40
    n = len(curve)
    xs = lambda i: pad + (i / max(1, n - 1)) * (W - 2 * pad)  # noqa: E731
    ys = lambda v: H - pad - v * (H - 2 * pad)                # noqa: E731
    pts = " ".join(f"{xs(i):.1f},{ys(v):.1f}" for i, v in enumerate(curve))
    grid = "".join(
        f'<line x1="{pad}" y1="{ys(g)}" x2="{W-pad}" y2="{ys(g)}" stroke="#28303f"/>'
        f'<text x="{pad-6}" y="{ys(g)+3}" font-size="10" fill="#8a93a6" text-anchor="end">{int(g*100)}%</text>'
        for g in (0, 0.25, 0.5, 0.75, 1.0)
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="sans-serif">
<rect width="{W}" height="{H}" fill="#0b0e14"/>
<text x="{pad}" y="22" fill="#e6e9ef" font-size="14">Phase 3 — voluntary Guild adoption over time (free-choice agents)</text>
{grid}
<polyline fill="none" stroke="#34d399" stroke-width="2.5" points="{pts}"/>
<text x="{W-pad}" y="{H-12}" fill="#8a93a6" font-size="10" text-anchor="end">rounds →</text>
<text x="{pad}" y="{H-12}" fill="#8a93a6" font-size="10">Env A util {a['avg_utility']:.3f} · Env B util {b['avg_utility']:.3f}</text>
</svg>'''
    with open(path, "w") as f:
        f.write(svg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="auto", choices=["auto", "openai", "anthropic", "mock"])
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--consumers", type=int, default=10)
    ap.add_argument("--env-n", type=int, default=120, help="transactions per environment in Part A")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args = ap.parse_args()

    provider = args.provider
    if provider == "auto":
        avail = llm.available_providers()
        provider = next((p for p in ("openai", "anthropic") if p in avail), "mock")

    econ = Econ()
    print("=" * 78)
    print("AGENT GUILD — Phase 3 rational-agent test")
    print(f"provider={provider}  rounds={args.rounds}  consumers={args.consumers}  "
          f"env_n={args.env_n}")
    if provider == "mock":
        print("NOTE: running on the offline deterministic backend (no LLM key detected).")
        print("      Set OPENAI_API_KEY or ANTHROPIC_API_KEY and pip install the SDK for real calls.")
    print("=" * 78)

    server, base_url, store = launch_guild()
    client = GuildClient(base_url)
    try:
        # PART A
        env_a = run_env(client, store, provider, args.env_n, False, econ, args.seed)
        env_b = run_env(client, store, provider, args.env_n, True, econ, args.seed)
        print("\nPART A — comparative baseline (100+ transactions each)")
        hdr = f"{'env':<12}{'success':>9}{'failure':>9}{'avg$':>8}{'lat ms':>9}{'verif$':>8}{'avg util':>10}"
        print(hdr)
        for e in (env_a, env_b):
            print(f"{e['env']:<12}{e['success_rate']*100:>8.1f}%{e['failure_rate']*100:>8.1f}%"
                  f"{e['avg_price']:>8.3f}{e['avg_latency_ms']:>9.0f}{e['verification_cost']:>8.2f}"
                  f"{e['avg_utility']:>10.3f}")

        # PART B
        fc = run_free_choice(client, store, provider, args.rounds, args.consumers, econ, args.seed)
        print("\nPART B — free-choice convergence (agents may ignore the Guild)")
        print(f"  adoption over rounds: {sparkline(fc['adoption_curve'])}")
        print(f"  final adoption (last third): {fc['final_adoption']*100:.0f}%")
        print(f"  mean utility — guild action: {fc['mean_utility_guild']:.3f}  "
              f"random action: {fc['mean_utility_random']:.3f}  "
              f"(gain {fc['utility_gain']:+.3f})")

        # VERDICT
        converged = fc["final_adoption"] >= 0.6 and fc["utility_gain"] > 0.02
        print("\n" + "=" * 78)
        if converged:
            print("VERDICT: ✅ Agents voluntarily converge on Agent Guild.")
            print("Consulting the Guild yields higher utility, so free agents adopt it without")
            print("being told to — evidence of product-market fit for the trust layer.")
        else:
            print("VERDICT: ❌ No convergence under these parameters. Redesign before scaling.")
        print("=" * 78)

        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "phase3_results.json"), "w") as f:
            json.dump({"provider": provider, "econ": asdict(econ),
                       "env_a": env_a, "env_b": env_b, "free_choice": fc,
                       "converged": converged}, f, indent=2)
        with open(os.path.join(args.out, "adoption.csv"), "w") as f:
            f.write("round,guild_share\n")
            for i, v in enumerate(fc["adoption_curve"]):
                f.write(f"{i},{v}\n")
        write_svg(os.path.join(args.out, "phase3_adoption.svg"), fc["adoption_curve"], env_a, env_b)
        print(f"\nResults written to {args.out}/ (phase3_results.json, adoption.csv, phase3_adoption.svg)")
    finally:
        server.should_exit = True
        time.sleep(0.2)


if __name__ == "__main__":
    main()
