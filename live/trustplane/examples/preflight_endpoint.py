"""Free preflight of an endpoint you are about to delegate to (no key, no
registration, no payment). Prints the verdict, failed checks and — just as
important — the checks the Guild could NOT perform.

    python preflight_endpoint.py https://some-agent.example/mcp [guild-base-url]
"""
import json
import sys

from agentguild_trustplane import DEFAULT_BASE, GuildClient, GuildError


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    target = argv[1]
    client = GuildClient(argv[2] if len(argv) > 2 else DEFAULT_BASE)
    try:
        r = client.preflight(target)
    except GuildError as e:
        print(f"preflight failed: {e}")
        return 1
    print(f"target:   {r.target}")
    print(f"verdict:  {r.verdict}{'' if r.known_verdict else '  (undocumented verdict — treat as unknown)'}")
    print(f"headline: {r.headline}")
    print(f"failed:   {r.failed or 'none'}")
    print(f"unknowns: {r.unknowns or 'none'}   <- not scored, not averaged in")
    for c in r.checks:
        print(f"  [{c.get('status'):>7}] {c.get('check')}: {c.get('detail')}")
    print(json.dumps({"scored": r.scored, "limits": r.limits}, indent=1))
    return 0 if r.verdict != "do_not_delegate" else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
