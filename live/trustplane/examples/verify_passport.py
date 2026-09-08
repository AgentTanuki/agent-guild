"""Fetch an agent's Guild-signed Passport and verify it BEFORE trusting a
single claim in it: signature, issuer, validity window, and that it is about
the identity you asked for. Free.

    python verify_passport.py <agent_id or did:key:...> [guild-base-url]

Or verify a passport you were handed (no network at all):

    python verify_passport.py --offline passport.json <expected did:key:... or agent id>
"""
import json
import sys

from agentguild_trustplane import DEFAULT_BASE, GuildClient, verify_data_integrity, within_validity
from agentguild_trustplane.client import _passport_subject_ok


def offline(path: str, expected_subject: str) -> int:
    doc = json.load(open(path, encoding="utf-8"))
    v = verify_data_integrity(doc)
    valid, age = within_validity(doc)
    binding = _passport_subject_ok(expected_subject, doc)
    print(json.dumps({"signature": v, "inside_validity_window": valid,
                      "age_seconds": age, "subject_binding": binding or "ok",
                      "issuer_pinned": False,
                      "note": "Signature and window verified offline. Whether the "
                              "issuer DID is one you trust is YOUR decision: pin it "
                              "(compare with GET /ledger/issuer) before relying on the claims."},
                     indent=1))
    return 0 if v["verified"] and valid and binding is None else 1


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[1] == "--offline":
        return offline(argv[2], argv[3])
    if len(argv) < 2:
        print(__doc__)
        return 2
    client = GuildClient(argv[2] if len(argv) > 2 else DEFAULT_BASE)
    r = client.passport_result(argv[1])
    print(f"channel: {r.channel}   ok: {r.ok}")
    if not r.ok:
        print(f"reason:  {r.reason}")
        return 1
    subject = r.doc["credentialSubject"]
    print(f"subject: {r.subject_did}   issuer: {r.issuer_did}   age: {r.age_seconds:.0f}s")
    print(f"validUntil: {r.doc.get('validUntil')}")
    print(json.dumps({k: subject.get(k) for k in ("name", "capabilities", "trust",
                                                    "confidence", "verified_task_count",
                                                    "recommendation", "risk")}, indent=1))
    print("The Guild's `recommendation` is its presentation; the threshold is yours.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
