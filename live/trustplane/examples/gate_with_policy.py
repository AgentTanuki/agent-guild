"""The gateway path: vet a delegation under YOUR policy, act only on an
allowed gate, and report the outcome. Uses signed decisions (`/check?signed=true`),
which the public Guild prices: with no cached decision the gate reports the 402
in its reasons and falls to your tier's fail mode — it never pays.

    python gate_with_policy.py <capability> [guild-base-url] [state-dir]

The signed cache and outcome queue live in state-dir (default ~/.agentguild).
"""
import sys

from agentguild_trustplane import DEFAULT_BASE, Gateway, RiskPolicy


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    gw = Gateway(policy=RiskPolicy(), base_url=argv[2] if len(argv) > 2 else DEFAULT_BASE,
                 state_dir=argv[3] if len(argv) > 3 else "~/.agentguild")
    gate = gw.gate(argv[1], value_at_risk=5.0)
    print(f"allowed={gate.allowed} channel={gate.channel} tier={gate.tier} "
          f"fail_state={gate.policy.fail_state}")
    for r in gate.policy.reasons:
        print(f"  - {r}")
    if gate.channel == "payment_required":
        pr = gw.client.last_payment_required
        print(f"quoted (not paid): {[t['amount'] for t in pr.terms]} -> {pr.path}")
    if gate.allowed and gate.routing and gate.routing.get("routable"):
        print(f"would invoke ONLY {gate.routing['endpoint']} and then gw.report(gate, 'accepted', ...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
