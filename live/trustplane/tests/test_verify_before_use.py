"""P0 regression: live evidence is VERIFIED BEFORE USE, issuer pins persist
across restart, and issuer changes require a verified rotation chain
(corrective pass 2026-07-13).
"""
from __future__ import annotations

import json
import time
import urllib.request

import pytest

from agentguild_trustplane.cache import SignedDecisionCache
from agentguild_trustplane.client import GuildClient
from agentguild_trustplane.gateway import Gateway
from agentguild_trustplane.policy import RiskPolicy


def _tamper(doc):
    bad = json.loads(json.dumps(doc))
    if bad.get("decision"):
        bad["decision"]["estimate"] = 0.99
    else:
        bad["status"] = bad.get("status", "") + "x"
    return bad


class _StubClientBase(GuildClient):
    """GuildClient whose HTTP layer is replaced by canned documents."""

    def __init__(self, docs_by_path, cache=None):
        super().__init__("http://stub", cache=cache)
        self._docs = docs_by_path

    def _get(self, path):
        for prefix, doc in self._docs.items():
            if path.startswith(prefix):
                if callable(doc):
                    return doc()
                return doc
        raise urllib.error.URLError("no stub")


@pytest.fixture()
def live_doc(guild_server, seeded):
    with urllib.request.urlopen(
            guild_server["base"] + "/check?capability=tp-echo&signed=true",
            timeout=15) as r:
        return json.loads(r.read().decode())


def test_tampered_live_document_is_never_channel_live(live_doc, tmp_path):
    cache = SignedDecisionCache(tmp_path / "c")
    client = _StubClientBase({"/check": _tamper(live_doc)}, cache=cache)
    doc, channel, _age = client.signed_decision("tp-echo")
    assert channel == "unverified" and doc is None
    assert client.stats["live_verify_failures"] == 1
    assert "proof" in (client.last_verify_failure or "")


def test_expired_live_document_is_rejected(guild_server, seeded, tmp_path):
    # ask the REAL guild for a min-TTL envelope, then age it out locally
    with urllib.request.urlopen(
            guild_server["base"] + "/check?capability=tp-echo&signed=true"
            "&ttl_seconds=60", timeout=15) as r:
        doc = json.loads(r.read().decode())
    doc = json.loads(json.dumps(doc))
    # an expired doc still has a VALID signature only if unmodified — so
    # build a stub whose clock has moved past valid_until instead
    import agentguild_trustplane.client as client_mod
    cache = SignedDecisionCache(tmp_path / "c")
    client = _StubClientBase({"/check": doc}, cache=cache)
    orig = client_mod.within_validity
    client_mod.within_validity = lambda d, now=None: (False, 99999.0)
    try:
        got, channel, _ = client.signed_decision("tp-echo")
    finally:
        client_mod.within_validity = orig
    assert channel == "unverified" and got is None
    assert "validity" in (client.last_verify_failure or "")


def test_unknown_issuer_rejected_against_persisted_pin(live_doc, tmp_path):
    cache = SignedDecisionCache(tmp_path / "c")
    client = _StubClientBase(
        {"/check": live_doc, "/ledger/rotations": {"rotations": []}},
        cache=cache)
    doc, channel, _ = client.signed_decision("tp-echo")
    assert channel == "live"                       # TOFU pin on first use
    pinned = list(cache.trusted_issuers)

    # RESTART: a fresh cache over the same directory reloads the pins
    cache2 = SignedDecisionCache(tmp_path / "c")
    assert cache2.trusted_issuers == pinned

    # a document signed by a DIFFERENT issuer (self-signed doppelganger)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from agentguild_trustplane.verify import (b58encode, canonicalize_jcs)
    import hashlib
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes_raw()
    did = "did:key:z" + b58encode(b"\xed\x01" + pub)
    forged = json.loads(json.dumps(live_doc))
    forged["issuer"] = did
    proof = dict(forged["proof"])
    proof["verificationMethod"] = f"{did}#{did.split(':')[-1]}"
    proof.pop("proofValue", None)
    cfg = hashlib.sha256(canonicalize_jcs(proof).encode()).digest()
    body = {k: v for k, v in forged.items() if k != "proof"}
    h = cfg + hashlib.sha256(canonicalize_jcs(body).encode()).digest()
    sig = priv.sign(h)
    proof["proofValue"] = "z" + b58encode(sig)
    forged["proof"] = proof

    client2 = _StubClientBase(
        {"/check": forged, "/ledger/rotations": {"rotations": []}},
        cache=cache2)
    doc2, channel2, _ = client2.signed_decision("tp-echo")
    # cryptographically valid, but the ISSUER is not the pinned one and no
    # rotation chain connects it -> rejected, never live
    assert channel2 in ("unverified", "cache")
    if channel2 == "cache":
        # served the OLD pinned issuer's cached doc, not the forged one
        assert doc2["issuer"] != did
    assert did not in cache2.trusted_issuers


def test_valid_issuer_rotation_is_accepted(guild_server, seeded, tmp_path):
    """Rotate the REAL local Guild's issuer key; the client must accept the
    new issuer ONLY via the dual-signed on-chain rotation entry."""
    store = guild_server["store"]
    old_did = store.guild_identity()["did"]
    cache = SignedDecisionCache(tmp_path / "c",
                                trusted_issuers=[old_did])   # pre-pinned
    client = GuildClient(guild_server["base"], cache=cache)
    doc, channel, _ = client.signed_decision("tp-echo")
    assert channel == "live" and doc["issuer"] == old_did

    store.rotate_guild_identity()
    new_did = store.guild_identity()["did"]
    assert new_did != old_did
    doc2, channel2, _ = client.signed_decision("tp-echo")
    assert channel2 == "live" and doc2["issuer"] == new_did
    assert new_did in cache.trusted_issuers
    assert cache.counters["rotations_accepted"] >= 1
    # and the acceptance PERSISTS across restart
    cache3 = SignedDecisionCache(tmp_path / "c")
    assert new_did in cache3.trusted_issuers


def test_unverified_never_falls_open_even_on_micro_tier(live_doc, tmp_path):
    """A tampered doc is an integrity signal: enforce mode denies even where
    an OUTAGE would fail open (micro tier)."""
    gw = Gateway(policy=RiskPolicy(), state_dir=tmp_path / "gw",
                 base_url="http://stub")
    gw.client = _StubClientBase({"/check": _tamper(live_doc)},
                                cache=gw.cache)
    gate = gw.gate("tp-echo", value_at_risk=1.0)   # micro: outage fails OPEN
    assert gate.channel == "unverified"
    assert not gate.allowed                        # ...but tamper fails CLOSED
    assert gate.policy.fail_state == "unverified"
