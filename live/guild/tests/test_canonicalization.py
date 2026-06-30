"""Lock the language-agnostic canonicalization (AGI-1 §2.1).

The whole cross-language verifier story depends on one rule: an integer-valued
number serialises with NO decimal point (0.0 -> "0"), matching ECMAScript, so a
credential signed in Python verifies byte-for-byte in JavaScript/Go/etc.
"""
import os

os.environ["GUILD_DATA"] = ""

from app.crypto import canonicalize_jcs, sign_jcs, verify_jcs, generate_keypair, did_from_public_key  # noqa: E402


def test_integer_valued_floats_have_no_decimal_point():
    assert canonicalize_jcs(0.0) == "0"        # NOT "0.0" (this is the bug class)
    assert canonicalize_jcs(44.0) == "44"
    assert canonicalize_jcs(44.2) == "44.2"
    assert canonicalize_jcs(0) == "0"
    assert canonicalize_jcs(True) == "true"


def test_sorted_keys_and_no_whitespace():
    assert canonicalize_jcs({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonicalize_jcs({"x": [1, 2.0, {"k": 0.0}]}) == '{"x":[1,2,{"k":0}]}'


def test_matches_javascript_json_stringify_shape():
    # The exact strings a JS canon (sorted keys + JSON.stringify primitives) yields.
    cases = {
        '{"collusion_suspicion":0.0,"trust":44.2}': '{"collusion_suspicion":0,"trust":44.2}',
    }
    obj = {"collusion_suspicion": 0.0, "trust": 44.2}
    assert canonicalize_jcs(obj) == '{"collusion_suspicion":0,"trust":44.2}'
    assert list(cases.values())[0] == canonicalize_jcs(obj)


def test_sign_and_verify_roundtrip():
    priv, pub = generate_keypair()
    payload = {"trust": 44.2, "collusion": 0.0, "n": 12, "ok": True, "tags": ["a", "b"]}
    sig = sign_jcs(payload, priv)
    assert verify_jcs(payload, sig, pub) is True
    payload["trust"] = 99.9
    assert verify_jcs(payload, sig, pub) is False
