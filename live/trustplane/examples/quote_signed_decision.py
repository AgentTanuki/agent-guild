"""Find out what a signed AGD-1 decision costs — WITHOUT paying.

The Guild prices `GET /check?signed=true` with an x402 v2 challenge (HTTP 402).
quote() sends NO api key and NO credential/payment headers, so it can neither
settle a payment nor debit sandbox credits; it reports the exact terms the
service quoted so the caller can decide, with its own wallet and policy.

    python quote_signed_decision.py <capability> [guild-base-url]
"""
import json
import sys

from agentguild_trustplane import DEFAULT_BASE, GuildClient, verify_data_integrity, within_validity


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
        # A free/lab instance served the document without credentials. It is
        # NOT verified by quote(); check it locally — no second request.
        doc = q["document"]
        v = verify_data_integrity(doc) if isinstance(doc, dict) else {"verified": False, "reason": "not an object"}
        fresh, age = within_validity(doc) if isinstance(doc, dict) else (False, None)
        print(f"served (unverified by quote); signature={v['verified']} ({v['reason']}) "
              f"issuer={v.get('issuer_did')} inside_window={fresh} age={age}")
        print("Issuer trust is your pin, not this program's. Use client.signed_decision() for the "
              "fully verified path (contract, binding, issuer policy).")
        print(json.dumps(doc, indent=1)[:1500])
        return 0
    print(f"error: {q['error']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
