"""Adoption-sensitivity experiment for Agent Guild.

Question: under what conditions does *voluntary* adoption of the Guild emerge,
and under what conditions does it fail? We vary four factors INDEPENDENTLY:

  1. guild_query_cost   — the utility drag of consulting the Guild
  2. learner aggressiveness/exploration — Bandit alpha (update rate) and eps0
  3. task difficulty     — how hard tasks are (lowers everyone's success)
  4. worker quality spread — how distinguishable good workers are from bad

and measure, per cell (averaged over many seeds):

  - final_adoption        (last-third mean of the free-choice adoption curve)
  - adoption_auc          (mean adoption across all rounds)
  - rounds_to_50          (convergence speed: first round trailing-mean >= 0.5)
  - guild_util / random_util / util_gain  (free-choice, per-action utility)
  - A_success / B_success / B_minus_A     (forced random vs forced Guild)
  - A_cost_per_success / B_cost_per_success
  - B_util / A_util

METHOD NOTE — this runs on the *real* EigenTrust reputation engine
(`app.reputation.score_agents`) and faithfully replicates the production
hiring-agent selection rule, the recency-weighted Bandit learner, and the
economic model. Only worker *quality* is synthetic — it MUST be, because
"difficulty" and "quality spread" cannot be set as independent variables with
real LLMs. Nothing here is tuned to make the Guild win: worker price is
correlated with quality plus noise (so the cheapest is usually the worst but
genuine cheap-good "value" workers and pricey-bad "traps" both occur), and the
Guild query is a real cost. We report where adoption fails as readily as where
it succeeds.

Usage:
  python experiments/adoption_sweep.py --axis baseline
  python experiments/adoption_sweep.py --axis query_cost --seeds 24
  python experiments/adoption_sweep.py --axis all --seeds 24
  python experiments/adoption_sweep.py --report     # build CSV + markdown
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from dataclasses import dataclass, asdict

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LIVE, "guild"))

from app.reputation import score_agents  # noqa: E402  (the real reputation engine)

CAP = "qa-agent"
OUT = os.path.join(HERE, "results")


# --------------------------------------------------------------------------- #
# Economics + selection — faithful copies of the production rule
# (Econ/utility from real_world.py; selection from hiring_agent.py)
# --------------------------------------------------------------------------- #
@dataclass
class Econ:
    reward: float = 1.0
    penalty: float = 1.5
    latency_cost: float = 0.05
    guild_query_cost: float = 0.02


@dataclass
class Sel:
    reward: float = 1.0
    penalty: float = 1.5
    latency_cost: float = 0.05
    explore: float = 0.1
    optimism: float = 0.35


def utility(correct, price, latency_ms, used_guild, e: Econ):
    u = (e.reward if correct else -e.penalty) - price - e.latency_cost * (latency_ms / 1000.0)
    if used_guild:
        u -= e.guild_query_cost
    return u


def _p_from_rep(r, w: Sel):
    t = r["trust"] / 100.0
    conf = r.get("confidence", 0.0)
    return min(0.98, max(0.02, 0.5 + (t - 0.5) * (0.5 + 0.5 * conf)))


def _eu(r, w: Sel):
    p = _p_from_rep(r, w)
    conf = r.get("confidence", 0.0)
    price = r["metadata"].get("price_per_call", 0.0)
    lat = r["metadata"].get("est_latency_ms", 0.0) / 1000.0
    eu = w.reward * p - w.penalty * (1 - p)
    eu -= conf * (price + w.latency_cost * lat)
    eu += w.optimism * (1 - conf)
    return eu


def _select(results, rng, w: Sel):
    if rng.random() < w.explore:
        return rng.choice(results)
    return max(results, key=lambda r: _eu(r, w))


# --------------------------------------------------------------------------- #
# Recency-weighted Bandit learner — faithful copy of real_world.py Bandit
# --------------------------------------------------------------------------- #
@dataclass
class Bandit:
    alpha: float = 0.3
    eps0: float = 0.3
    q_guild: float = 0.6
    q_random: float = 0.6
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
            self.tg = True
            self.q_guild += self.alpha * (u - self.q_guild)
        else:
            self.tr = True
            self.q_random += self.alpha * (u - self.q_random)


# --------------------------------------------------------------------------- #
# In-process Guild shim: real reputation engine, no HTTP / no crypto overhead.
# Crypto signing is bypassed (it does not affect ratings or scores, only speed),
# so the reputation DYNAMICS are identical to production.
# --------------------------------------------------------------------------- #
class Guild:
    def __init__(self):
        self.agents: dict[str, dict] = {}
        self.edges: list[tuple[str, str, float]] = []
        self.seeds: list[str] = []
        self._cache = None

    def register(self, name, metadata, seed=False):
        aid = "agent_%03d" % len(self.agents)
        self.agents[aid] = {"id": aid, "name": name, "metadata": metadata}
        if seed:
            self.seeds.append(aid)
        self._cache = None
        return aid

    def attest(self, issuer, subject, rating):
        self.edges.append((issuer, subject, float(rating)))
        self._cache = None

    def _scores(self):
        if self._cache is None:
            self._cache = score_agents(list(self.agents), self.edges, self.seeds).scores
        return self._cache

    def search(self, worker_ids):
        sc = self._scores()
        res = []
        for aid in worker_ids:
            s = sc.get(aid)
            res.append({
                "id": aid,
                "metadata": self.agents[aid]["metadata"],
                "trust": s.trust if s else 0.0,
                "confidence": s.confidence if s else 0.0,
            })
        res.sort(key=lambda x: x["trust"], reverse=True)
        return res


# --------------------------------------------------------------------------- #
# Synthetic worker pool — the ONLY synthetic part. Quality is set by (center,
# spread); difficulty lowers realized success. Price ~ quality + noise, so the
# cheapest is usually (not always) the worst — a genuine quality/cost tradeoff.
# --------------------------------------------------------------------------- #
def build_workers(guild, n, center, spread, difficulty, rng):
    if n > 1:
        qs = [center - spread / 2 + spread * i / (n - 1) for i in range(n)]
    else:
        qs = [center]
    workers = []
    for i, q in enumerate(qs):
        frac = 0.0 if spread == 0 else (q - (center - spread / 2)) / spread
        price = round(max(0.002, (0.004 + 0.03 * frac) * rng.uniform(0.6, 1.4)), 4)
        lat = max(150.0, (400 + 1200 * frac) * rng.uniform(0.7, 1.3))
        aid = guild.register(f"W{i}", {"price_per_call": price, "est_latency_ms": lat}, seed=False)
        p_succ = min(0.98, max(0.02, q - difficulty))
        workers.append({"id": aid, "p": p_succ, "price": price, "lat": lat})
    return workers


def _observe(worker, rng):
    correct = rng.random() < worker["p"]
    lat = worker["lat"] * rng.uniform(0.85, 1.15)
    return correct, lat


# --------------------------------------------------------------------------- #
# Conditions
# --------------------------------------------------------------------------- #
def _wmap(workers):
    return {w["id"]: w for w in workers}


def run_A(cfg, econ, sel, seed):
    """Forced random selection."""
    g = Guild()
    rng = random.Random(seed)
    workers = build_workers(g, cfg["n_workers"], cfg["center"], cfg["spread"], cfg["difficulty"], rng)
    wm = _wmap(workers)
    ids = [w["id"] for w in workers]
    g.register("Hirer-A", {}, seed=True)
    n = cfg["forced_n"]
    correct = spend = 0.0
    util = 0.0
    for _ in range(n):
        w = wm[rng.choice(ids)]
        ok, lat = _observe(w, rng)
        correct += 1 if ok else 0
        spend += w["price"]
        util += utility(ok, w["price"], lat, False, econ)
    return {"success": correct / n, "util": util / n,
            "cost_per_success": (spend / correct) if correct else float("nan")}


def run_B(cfg, econ, sel, seed):
    """Forced Guild selection (reputation-ranked)."""
    g = Guild()
    rng = random.Random(seed + 1)
    workers = build_workers(g, cfg["n_workers"], cfg["center"], cfg["spread"], cfg["difficulty"], rng)
    wm = _wmap(workers)
    ids = [w["id"] for w in workers]
    hirer = g.register("Hirer-B", {}, seed=True)
    n = cfg["forced_n"]
    correct = spend = 0.0
    util = 0.0
    for _ in range(n):
        results = g.search(ids)
        chosen = _select(results, rng, sel) if results else None
        wid = chosen["id"] if chosen else rng.choice(ids)
        w = wm[wid]
        ok, lat = _observe(w, rng)
        g.attest(hirer, wid, 1.0 if ok else 0.0)
        correct += 1 if ok else 0
        spend += w["price"] + econ.guild_query_cost
        util += utility(ok, w["price"], lat, True, econ)
    return {"success": correct / n, "util": util / n,
            "cost_per_success": (spend / correct) if correct else float("nan")}


def run_C(cfg, econ, sel, seed):
    """Free choice: each consumer learns whether to consult the Guild."""
    g = Guild()
    rng = random.Random(seed + 2)
    workers = build_workers(g, cfg["n_workers"], cfg["center"], cfg["spread"], cfg["difficulty"], rng)
    wm = _wmap(workers)
    ids = [w["id"] for w in workers]
    consumers = [g.register(f"Hirer-C{i}", {}, seed=True) for i in range(cfg["consumers"])]
    bandits = [Bandit(alpha=cfg["alpha"], eps0=cfg["eps0"]) for _ in consumers]
    adoption = []
    gu = ru = 0.0
    gn = rn = 0
    for _ in range(cfg["rounds"]):
        used = 0
        for cid, b in zip(consumers, bandits):
            ug = b.choose(rng)
            if ug:
                results = g.search(ids)
                chosen = _select(results, rng, sel) if results else None
                wid = chosen["id"] if chosen else rng.choice(ids)
            else:
                wid = rng.choice(ids)
            w = wm[wid]
            ok, lat = _observe(w, rng)
            g.attest(cid, wid, 1.0 if ok else 0.0)  # both paths enrich the graph
            u = utility(ok, w["price"], lat, ug, econ)
            b.update(ug, u)
            if ug:
                gu += u; gn += 1; used += 1
            else:
                ru += u; rn += 1
        adoption.append(used / len(consumers))
    R = len(adoption)
    third = max(1, R // 3)
    final_adoption = sum(adoption[-third:]) / third
    auc = sum(adoption) / R
    # early adoption AFTER the learner's forced 1st-guild/2nd-random bootstrap
    early = adoption[3:9] or adoption
    early_adoption = sum(early) / len(early)
    # convergence speed = settling round: earliest r from which the trailing-3
    # mean stays within 0.12 of the final level for the rest of the run.
    def trail3(i):
        win = adoption[max(0, i - 2):i + 1]
        return sum(win) / len(win)
    settle = R
    for i in range(R - 1, -1, -1):
        if abs(trail3(i) - final_adoption) <= 0.12:
            settle = i + 1
        else:
            break
    return {
        "final_adoption": final_adoption,
        "early_adoption": early_adoption,
        "adoption_auc": auc,
        "settling_round": settle,
        "guild_util": (gu / gn) if gn else float("nan"),
        "random_util": (ru / rn) if rn else float("nan"),
        "util_gain": ((gu / gn) if gn else 0.0) - ((ru / rn) if rn else 0.0),
        "adoption_curve": adoption,
    }


# --------------------------------------------------------------------------- #
# Cell = one parameter configuration, averaged over seeds
# --------------------------------------------------------------------------- #
def _mean(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return sum(xs) / len(xs) if xs else float("nan")


def run_cell(cfg, seeds):
    econ = Econ(guild_query_cost=cfg["query_cost"])
    sel = Sel()
    A = [run_A(cfg, econ, sel, s) for s in range(seeds)]
    B = [run_B(cfg, econ, sel, s) for s in range(seeds)]
    C = [run_C(cfg, econ, sel, s) for s in range(seeds)]
    a_succ = _mean([r["success"] for r in A]); b_succ = _mean([r["success"] for r in B])
    a_u = _mean([r["util"] for r in A]); b_u = _mean([r["util"] for r in B])
    fin = _mean([r["final_adoption"] for r in C]); ear = _mean([r["early_adoption"] for r in C])
    return {
        "final_adoption": fin,
        "early_adoption": ear,
        "adoption_trend": fin - ear,
        "adoption_auc": _mean([r["adoption_auc"] for r in C]),
        "settling_round": _mean([r["settling_round"] for r in C]),
        "guild_util": _mean([r["guild_util"] for r in C]),
        "random_util": _mean([r["random_util"] for r in C]),
        "util_gain": _mean([r["util_gain"] for r in C]),
        "A_success": a_succ, "B_success": b_succ, "B_minus_A": b_succ - a_succ,
        "A_util": a_u, "B_util": b_u,
        "A_cost_per_success": _mean([r["cost_per_success"] for r in A]),
        "B_cost_per_success": _mean([r["cost_per_success"] for r in B]),
    }


# --------------------------------------------------------------------------- #
# Sweep definition
# --------------------------------------------------------------------------- #
BASE = dict(query_cost=0.02, alpha=0.3, eps0=0.3, difficulty=0.15, spread=0.5,
            center=0.72, n_workers=6, rounds=40, consumers=8, forced_n=48)

AXES = {
    "query_cost": [0.0, 0.01, 0.02, 0.05, 0.10, 0.20],
    "alpha":      [0.1, 0.2, 0.3, 0.5, 0.8],
    "eps0":       [0.05, 0.1, 0.2, 0.3, 0.5],
    "difficulty": [0.0, 0.1, 0.2, 0.3, 0.45],
    "spread":     [0.0, 0.1, 0.2, 0.4, 0.6, 0.8],
}
GRID_DIFF = [0.0, 0.15, 0.3, 0.45]
GRID_SPREAD = [0.0, 0.2, 0.4, 0.7]


def cfg_with(**over):
    c = dict(BASE); c.update(over); return c


def run_axis(axis, seeds):
    rows = []
    if axis == "baseline":
        rows.append({"axis": "baseline", "value": None, **run_cell(cfg_with(), seeds)})
    elif axis == "grid":
        for d in GRID_DIFF:
            for s in GRID_SPREAD:
                rows.append({"axis": "grid", "difficulty": d, "spread": s,
                             **run_cell(cfg_with(difficulty=d, spread=s), seeds)})
    elif axis in AXES:
        for v in AXES[axis]:
            rows.append({"axis": axis, "value": v, **run_cell(cfg_with(**{axis: v}), seeds)})
    else:
        raise SystemExit(f"unknown axis {axis}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", default="baseline")
    ap.add_argument("--seeds", type=int, default=24)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    if args.report:
        build_report()
        return

    axes = list(AXES) + ["grid"] if args.axis == "all" else [args.axis]
    for ax in axes:
        rows = run_axis(ax, args.seeds)
        path = os.path.join(OUT, f"sweep_{ax}.json")
        with open(path, "w") as f:
            json.dump({"axis": ax, "seeds": args.seeds, "base": BASE, "rows": rows}, f, indent=2)
        print(f"[{ax}] {len(rows)} cells x {args.seeds} seeds -> {os.path.basename(path)}")
        for r in rows:
            tag = r.get("value", f"d{r.get('difficulty')}/s{r.get('spread')}")
            print(f"   {ax}={tag}: adopt={r['final_adoption']:.2f} "
                  f"trend={r['adoption_trend']:+.2f} gain={r['util_gain']:+.3f} "
                  f"B-A={r['B_minus_A']:+.2f} settle={r['settling_round']:.0f}")


# --------------------------------------------------------------------------- #
# Report builder
# --------------------------------------------------------------------------- #
def _load(ax):
    path = os.path.join(OUT, f"sweep_{ax}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def build_report():
    import csv
    all_rows = []
    for ax in ["baseline"] + list(AXES) + ["grid"]:
        d = _load(ax)
        if d:
            all_rows.extend(d["rows"])
    # tidy CSV
    cols = ["axis", "value", "difficulty", "spread", "final_adoption", "early_adoption",
            "adoption_trend", "adoption_auc", "settling_round", "util_gain", "guild_util",
            "random_util", "A_success", "B_success", "B_minus_A", "A_util", "B_util",
            "A_cost_per_success", "B_cost_per_success"]
    with open(os.path.join(OUT, "adoption_sweep.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"wrote adoption_sweep.csv ({len(all_rows)} rows)")
    print("Report data ready; see ADOPTION_SWEEP_FINDINGS.md (written separately).")


if __name__ == "__main__":
    main()
