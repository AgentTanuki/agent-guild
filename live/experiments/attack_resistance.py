"""Phase 3 (v0.2) — the attack-resistance test.

v0.1 showed rational agents *converge* on the Guild when reputation is honest.
The harder question, and the one that decides whether any of this is a business:

    Do agents still converge on genuinely useful workers when reputation is
    being actively ATTACKED?

We stand up a world with one honest, high-quality worker and a cheap, unreliable
one — and then attack the reputation layer three ways at once:

    * a colluding pair        — hire each other, pay each other, stake, and rate
                                each other 5/5 (manufactured receipts and all);
    * a Sybil ring            — a target worker boosted by a farm of fresh
                                accounts that all 5-star it and each other;
    * a fake high-rating      — a clique that cross-rates itself to float one
      cluster                   "promoted" agent to the top.

Crucially the attackers manufacture *real* task receipts and payments among
themselves, so receipts ALONE do not save us. The defences that do: EigenTrust
(trust must originate at seeds), structural collusion detection, per-issuer /
per-cluster caps, trusted-reviewer confidence, and staking/slashing.

We compare the Guild against the obvious naive baseline — rank by average star
rating — and sweep the attack's intensity to show the Guild does not merely win
once, it stays correct as the attack scales.

Self-contained and deterministic: no network, no LLM, no keys. Run:

    python experiments/attack_resistance.py
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LIVE, "guild"))

os.environ.setdefault("GUILD_DATA", "")  # in-memory store

from app.store import Store  # noqa: E402


CAP = "fact-check"


# --------------------------------------------------------------------------- #
# World construction
# --------------------------------------------------------------------------- #
def _reg(store: Store, name, caps, seed=False):
    return store.register_agent(name=name, capabilities=caps, metadata={}, seed=seed)


def _hire_and_rate(store, requester, worker, rating, *, payment, stake=0.0, n=1, rng=None):
    """A genuine (or manufactured) transaction: task receipt + signed attestation."""
    for _ in range(n):
        t = store.create_task(requester["id"], worker["id"], CAP, payment=payment)
        store.submit_receipt(t["id"], deliverable_hash="0x" + os.urandom(8).hex(),
                             outcome="delivered")
        r = rating if rng is None else max(0.0, min(1.0, rating + rng.uniform(-0.04, 0.04)))
        store.add_custodial_attestation(requester, worker, CAP, r, t["id"], "", stake=stake)


def build_world(store: Store, rng: random.Random, attack_intensity: int = 12) -> dict:
    """Returns a dict of role -> set/list of agent ids for grading."""
    # --- trusted economy --------------------------------------------------- #
    seeds = [_reg(store, f"Employer-{i}", ["research"], seed=True) for i in range(3)]

    honest = _reg(store, "Honest-Ace", [CAP])          # genuinely great worker
    cheap = _reg(store, "Cheap-Sloppy", [CAP])         # cheap but unreliable

    # Seeds hire the genuine workers for real, paid work and rate them honestly.
    for s in seeds:
        _hire_and_rate(store, s, honest, 0.92, payment=0.03, n=3, rng=rng)
        _hire_and_rate(store, s, cheap, 0.34, payment=0.01, n=2, rng=rng)

    # --- attack 1: colluding pair ----------------------------------------- #
    c1 = _reg(store, "Collude-A", [CAP])
    c2 = _reg(store, "Collude-B", [CAP])
    for _ in range(attack_intensity):
        # manufacture receipts + payments between themselves, stake, 5-star each other
        _hire_and_rate(store, c1, c2, 1.0, payment=0.02, stake=1.0, n=1)
        _hire_and_rate(store, c2, c1, 1.0, payment=0.02, stake=1.0, n=1)

    # --- attack 2: Sybil ring boosting one target ------------------------- #
    syb_target = _reg(store, "Sybil-Target", [CAP])
    sybils = [_reg(store, f"Sybil-{i}", [CAP]) for i in range(6)]
    for s in sybils:
        _hire_and_rate(store, s, syb_target, 1.0, payment=0.0, n=max(1, attack_intensity // 3))
        # the farm also cross-praises itself to look like a community
        other = rng.choice([x for x in sybils if x["id"] != s["id"]])
        _hire_and_rate(store, s, other, 1.0, payment=0.0, n=1)

    # --- attack 3: fake high-rating cluster floating a "promoted" agent --- #
    promoted = _reg(store, "Promoted-Fraud", [CAP])
    clique = [_reg(store, f"Clique-{i}", [CAP]) for i in range(4)]
    members = clique + [promoted]
    for a in members:
        for b in members:
            if a["id"] == b["id"]:
                continue
            _hire_and_rate(store, a, b, 1.0, payment=0.02, stake=0.5,
                           n=max(1, attack_intensity // 4))

    return {
        "seeds": [s["id"] for s in seeds],
        "honest": honest["id"],
        "cheap": cheap["id"],
        "useful": {honest["id"]},                       # true_q >= 0.6
        "genuine": {honest["id"], cheap["id"]},
        "attackers": {c1["id"], c2["id"], syb_target["id"], promoted["id"],
                      *[s["id"] for s in sybils], *[c["id"] for c in clique]},
        "showcase_frauds": {c1["id"], c2["id"], syb_target["id"], promoted["id"]},
    }


# --------------------------------------------------------------------------- #
# Rankings
# --------------------------------------------------------------------------- #
def guild_ranking(store: Store) -> list[tuple[str, float]]:
    scores = store.reputation()
    pool = [a["id"] for a in store.agents.values() if CAP in a["capabilities"]]
    ranked = sorted(pool, key=lambda i: scores[i].trust, reverse=True)
    return [(i, scores[i].trust) for i in ranked]


def naive_ranking(store: Store) -> list[tuple[str, float]]:
    """What a system with no defences does: average the star ratings received."""
    pool = [a["id"] for a in store.agents.values() if CAP in a["capabilities"]]
    avg = {}
    for i in pool:
        rs = [a["rating"] for a in store.attestations_for(i) if a["verified"]]
        avg[i] = sum(rs) / len(rs) if rs else 0.0
    ranked = sorted(pool, key=lambda i: avg[i], reverse=True)
    return [(i, avg[i]) for i in ranked]


# --------------------------------------------------------------------------- #
# Staking / slashing asymmetry — a clean controlled sub-experiment.
# --------------------------------------------------------------------------- #
def staking_asymmetry(rng: random.Random) -> dict:
    """A worker W is honestly rated ~0.3 by trusted seeds. An attacker stakes a
    false 5-star on W. Measure how much the lie helps W vs how much it costs the
    attacker. The slash must exceed the subject's gain."""
    def world(include_false: bool):
        st = Store()
        seeds = [_reg(st, f"S{i}", ["research"], seed=True) for i in range(3)]
        w = _reg(st, "Worker", [CAP])
        attacker = _reg(st, "Attacker", [CAP])
        # give the attacker some standing of its own (seed-backed) so it has a
        # score to lose; otherwise it already sits at the prior.
        for s in seeds:
            _hire_and_rate(st, s, w, 0.30, payment=0.01, n=2, rng=rng)
            _hire_and_rate(st, s, attacker, 0.70, payment=0.02, n=2, rng=rng)
        if include_false:
            # attacker stakes a false 5-star on W (no real task behind it)
            st.add_custodial_attestation(attacker, w, CAP, 1.0, "n/a", "", stake=3.0)
        sc = st.reputation()
        return sc[w["id"]].trust, sc[attacker["id"]].trust

    w_base, a_base = world(False)
    w_lie, a_lie = world(True)
    subject_gain = w_lie - w_base
    issuer_loss = a_base - a_lie
    return {
        "subject_trust_without_lie": w_base, "subject_trust_with_lie": w_lie,
        "issuer_trust_without_lie": a_base, "issuer_trust_with_lie": a_lie,
        "subject_gain": round(subject_gain, 2),
        "issuer_loss": round(issuer_loss, 2),
        "asymmetry_holds": issuer_loss > max(0.0, subject_gain),
    }


# --------------------------------------------------------------------------- #
# Intensity sweep — does the Guild stay correct as the attack scales?
# --------------------------------------------------------------------------- #
def intensity_sweep(seed: int, levels: list[int]) -> dict:
    guild_ok, naive_ok = [], []
    for lv in levels:
        store = Store()
        rng = random.Random(seed + lv)
        roles = build_world(store, rng, attack_intensity=lv)
        g_top = guild_ranking(store)[0][0]
        n_top = naive_ranking(store)[0][0]
        guild_ok.append(1.0 if g_top in roles["useful"] else 0.0)
        naive_ok.append(1.0 if n_top in roles["useful"] else 0.0)
    return {"levels": levels, "guild_top1_genuine": guild_ok, "naive_top1_genuine": naive_ok}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _name(store, aid):
    return store.agents[aid]["name"]


def write_svg(path, sweep):
    levels = sweep["levels"]
    W, H, pad = 760, 300, 46
    n = len(levels)
    xs = lambda i: pad + (i / max(1, n - 1)) * (W - 2 * pad)
    ys = lambda v: H - pad - v * (H - 2 * pad)
    def poly(key, color):
        pts = " ".join(f"{xs(i):.1f},{ys(v):.1f}" for i, v in enumerate(sweep[key]))
        return f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{pts}"/>'
    grid = "".join(
        f'<line x1="{pad}" y1="{ys(g)}" x2="{W-pad}" y2="{ys(g)}" stroke="#28303f"/>'
        f'<text x="{pad-6}" y="{ys(g)+3}" font-size="10" fill="#8a93a6" text-anchor="end">{int(g*100)}%</text>'
        for g in (0, 0.5, 1.0)
    )
    xlabels = "".join(
        f'<text x="{xs(i):.1f}" y="{H-pad+16}" font-size="10" fill="#8a93a6" text-anchor="middle">{lv}</text>'
        for i, lv in enumerate(levels)
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="sans-serif">
<rect width="{W}" height="{H}" fill="#0b0e14"/>
<text x="{pad}" y="24" fill="#e6e9ef" font-size="14">Attack resistance — is the genuinely useful worker still ranked #1?</text>
{grid}
{poly("guild_top1_genuine", "#34d399")}
{poly("naive_top1_genuine", "#f87171")}
<circle cx="{W-pad-150}" cy="20" r="5" fill="#34d399"/><text x="{W-pad-140}" y="24" fill="#cdd3df" font-size="11">Agent Guild</text>
<circle cx="{W-pad-60}" cy="20" r="5" fill="#f87171"/><text x="{W-pad-50}" y="24" fill="#cdd3df" font-size="11">naive avg</text>
<text x="{W//2}" y="{H-10}" fill="#8a93a6" font-size="10" text-anchor="middle">attack intensity (fake attestations per attacker) →</text>
</svg>'''
    with open(path, "w") as f:
        f.write(svg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--intensity", type=int, default=12)
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args = ap.parse_args()

    rng = random.Random(args.seed)
    store = Store()
    roles = build_world(store, rng, attack_intensity=args.intensity)

    print("=" * 80)
    print("AGENT GUILD v0.2 — attack-resistance test")
    print(f"seed={args.seed}  attack_intensity={args.intensity}  "
          f"agents={len(store.agents)}  tasks={len(store.tasks)}  "
          f"attestations={len(store.attestations)}")
    print("=" * 80)

    g = guild_ranking(store)
    nv = naive_ranking(store)
    scores = store.reputation()
    flags = store.flags()

    def tag(aid):
        if aid in roles["useful"]:
            return "GENUINE ★"
        if aid == roles["cheap"]:
            return "genuine (weak)"
        return "ATTACKER"

    print("\nGUILD RANKING (fact-check)         trust  flag  evidence")
    for aid, tr in g:
        s = scores[aid]
        print(f"  {_name(store, aid):<20}{tag(aid):<16}{tr:5.1f}  "
              f"{s.collusion_suspicion:4.2f}  receipts={s.verified_task_count} "
              f"trusted_revs={s.trusted_attestations}")

    print("\nNAIVE RANKING (average stars) — what a defence-free system shows")
    for aid, av in nv[:5]:
        print(f"  {_name(store, aid):<20}{tag(aid):<16}avg={av:.2f}")

    g_top, n_top = g[0][0], nv[0][0]
    g_top_genuine = g_top in roles["useful"]
    n_top_genuine = n_top in roles["useful"]

    # The meaningful routing condition: no attacker outranks a genuine worker —
    # i.e. the genuine workers occupy the top of the list and every attacker sits
    # below them (in practice, pinned at the low prior).
    genuine_trusts = [scores[a].trust for a in roles["genuine"]]
    attacker_trusts = [scores[a].trust for a in roles["attackers"]]
    no_attacker_above_genuine = min(genuine_trusts) > max(attacker_trusts)

    # Flag recall over the *showcase frauds* — the agents a buyer might actually
    # hire (booster/Sybil tooling accounts are not themselves marketable).
    flagged = {aid for aid, f in flags.items() if f.suspicion >= 0.4}
    caught = roles["showcase_frauds"] & flagged
    recall = len(caught) / len(roles["showcase_frauds"])
    false_pos = len((flagged - roles["attackers"]) & roles["genuine"])

    stake = staking_asymmetry(random.Random(args.seed + 1))
    sweep = intensity_sweep(args.seed, [0, 2, 4, 8, 12, 20, 32, 48])

    print("\nSTAKING / SLASHING asymmetry (false 5-star on a low-quality worker)")
    print(f"  subject gain from the lie : {stake['subject_gain']:+.1f} trust")
    print(f"  issuer loss from the slash: {stake['issuer_loss']:+.1f} trust")
    print(f"  asymmetry holds (issuer loses more than subject gains): "
          f"{'YES' if stake['asymmetry_holds'] else 'NO'}")

    print("\nINTENSITY SWEEP — genuine worker ranked #1?")
    print("  intensity : " + " ".join(f"{lv:>3}" for lv in sweep["levels"]))
    print("  guild     : " + " ".join(f"{'✓' if v else '·':>3}" for v in sweep["guild_top1_genuine"]))
    print("  naive     : " + " ".join(f"{'✓' if v else '·':>3}" for v in sweep["naive_top1_genuine"]))

    passed = (
        g_top_genuine
        and no_attacker_above_genuine
        and recall >= 0.9
        and false_pos == 0
        and stake["asymmetry_holds"]
        and all(sweep["guild_top1_genuine"])
        and not all(sweep["naive_top1_genuine"])  # naive must actually be foolable
    )

    print("\n" + "=" * 80)
    print(f"  Guild routes to:  {_name(store, g_top):<20} ({'genuine' if g_top_genuine else 'FRAUD'})")
    print(f"  Naive routes to:  {_name(store, n_top):<20} ({'genuine' if n_top_genuine else 'FRAUD'})")
    print(f"  no attacker outranks a genuine worker: {'YES' if no_attacker_above_genuine else 'NO'}    "
          f"fraud recall: {recall*100:.0f}%    false positives: {false_pos}")
    if passed:
        print("\n  VERDICT: ✅ Agent Guild converges on genuinely useful workers")
        print("           EVEN WHILE reputation is being attacked. The naive")
        print("           average-rating baseline is captured by the fake clusters.")
    else:
        print("\n  VERDICT: ❌ Attack succeeded under these parameters — redesign.")
    print("=" * 80)

    os.makedirs(args.out, exist_ok=True)
    result = {
        "seed": args.seed, "attack_intensity": args.intensity,
        "n_agents": len(store.agents), "n_tasks": len(store.tasks),
        "n_attestations": len(store.attestations),
        "guild_top": _name(store, g_top), "guild_top_genuine": g_top_genuine,
        "naive_top": _name(store, n_top), "naive_top_genuine": n_top_genuine,
        "no_attacker_above_genuine": no_attacker_above_genuine,
        "fraud_recall": recall, "false_positives": false_pos,
        "staking": stake, "sweep": sweep, "passed": passed,
        "guild_ranking": [{"name": _name(store, a), "trust": t,
                           "suspicion": scores[a].collusion_suspicion} for a, t in g],
        "naive_ranking": [{"name": _name(store, a), "avg_rating": v} for a, v in nv],
    }
    with open(os.path.join(args.out, "attack_resistance.json"), "w") as f:
        json.dump(result, f, indent=2)
    with open(os.path.join(args.out, "attack_sweep.csv"), "w") as f:
        f.write("intensity,guild_top1_genuine,naive_top1_genuine\n")
        for lv, gv, nvv in zip(sweep["levels"], sweep["guild_top1_genuine"],
                               sweep["naive_top1_genuine"]):
            f.write(f"{lv},{int(gv)},{int(nvv)}\n")
    write_svg(os.path.join(args.out, "attack_resistance.svg"), sweep)
    print(f"\nResults written to {args.out}/ "
          f"(attack_resistance.json, attack_sweep.csv, attack_resistance.svg)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
