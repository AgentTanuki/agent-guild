#!/usr/bin/env python3
"""Replay and verify Agent Guild's public discovery census proof."""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def b58decode(value):
    number = 0
    for char in value:
        number = number * 58 + ALPHABET.index(char)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") \
        if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + raw


def public_key_from_did(did):
    if not did.startswith("did:key:z"):
        raise ValueError("proof key is not an Ed25519 did:key")
    raw = b58decode(did.removeprefix("did:key:z"))
    if raw[:2] != bytes((0xED, 0x01)) or len(raw[2:]) != 32:
        raise ValueError("did:key is not Ed25519")
    return raw[2:]


def get_json(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "agent-guild-census-verifier/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verify(base):
    base = base.rstrip("/")
    report = get_json(base + "/discovery/reach")
    snapshot_events = report["proof"]["payload"]["event_snapshot_rows"]
    rows = []
    offset = 0
    proof = None
    expected_digest = None
    while True:
        query = urllib.parse.urlencode({
            "offset": offset, "limit": 2000,
            "snapshot_events": snapshot_events,
        })
        page = get_json(base + "/discovery/reach/evidence?" + query)
        rows.extend(page["actor_evidence"])
        proof = page["proof"]
        expected_digest = page["actor_evidence_set_sha256"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    digest = hashlib.sha256(canonical(rows).encode()).hexdigest()
    if digest != expected_digest:
        raise ValueError("actor evidence commitment mismatch")
    if len(rows) != proof["payload"]["actor_evidence_rows"]:
        raise ValueError("actor evidence row count mismatch")
    tiers = {
        "T1_key_proved_members": sum(
            row["tier"] == "T1_key_proved_member" for row in rows),
        "T2_named_mcp_clients": sum(
            row["tier"] == "T2_named_mcp_client" for row in rows),
        "T3_framework_ua_actors": sum(
            row["tier"] == "T3_framework_ua_actor" for row in rows),
    }
    if tiers != proof["payload"]["tiers"]:
        raise ValueError("tier counts do not replay")
    if proof["payload"] != report["proof"]["payload"]:
        raise ValueError("summary and evidence endpoints disagree")
    key = Ed25519PublicKey.from_public_bytes(
        public_key_from_did(proof["verification_key"]))
    key.verify(bytes.fromhex(proof["signature"]),
               canonical(proof["payload"]).encode())
    return {
        "valid": True,
        "qualified_distinct_autonomous_agents": len(rows),
        "tiers": tiers,
        "target_achieved": proof["payload"]["target_achieved"],
        "actor_evidence_set_sha256": digest,
        "rules_commit": proof["payload"]["rules_commit"],
        "measurement_history_complete": proof["payload"][
            "measurement_history_complete"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base", default="https://agent-guild-5d5r.onrender.com")
    args = parser.parse_args()
    print(json.dumps(verify(args.base), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
