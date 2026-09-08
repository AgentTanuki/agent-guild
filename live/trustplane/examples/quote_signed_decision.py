"""Find out what a signed AGD-1 decision costs — WITHOUT paying.

The Guild prices `GET /check?signed=true` with an x402 v2 challenge (HTTP 402).
This library never pays: it reports the exact terms the service quoted so the
caller can decide, with its own wallet and its own policy, whether to.

    python quote_signed_decision.py <capability> [guild-base-url]
"""
import json
import sys

from agentguild_trustplane import DEFAULT_BASE, GuildClient


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    client = GuildClient(argv[2] if len(argv) > 2 else DEFAULT_BASE)
    q = client.quote(argv[1], signed=True)
    print(f"status: {q['status']}")
    if q["status"] == "payment_required":
        print(f"resource: {q['resource']}")
        for t in q["terms"]:
            print(f"  quoted: {t['amount']} atomic units of {t['asset']} on {t['network']} "
                  f"({t['scheme']}) to {t['payTo']}, valid {t['maxTimeoutSeconds']}s")
        print("Nothing was paid. Amounts come from the response, never from this program.")
        return 0
    if q["status"] == "served":
        # A free/lab instance served the document. Still verify before use:
        env, channel, age = client.signed_decision(argv[1])
        print(f"served; verified channel={channel} age={age}")
        print(json.dumps(q["document"], indent=1)[:1500])
        return 0
    print(f"error: {q['error']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
