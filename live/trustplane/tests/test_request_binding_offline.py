"""Request ↔ envelope capability binding (Root reproduction on 148554af,
PYTHON-PACKAGE-REQUEST-BINDING-REPRO.json): a valid signed AgentGuildDecision
for capability 'translation' was returned as channel="live" for
signed_decision("code-review") and pinned the issuer; cache.put under the
wrong capability succeeded and the cached fallback served 'translation' for
'code-review'. ``binding_violations`` only ever compared decision ↔ envelope,
never the REQUEST ↔ envelope.

Offline: loopback fake Guild, synthetic issuer keys, a conformant + routable
AGD-1 envelope as the positive control. No network, no payment, no identity.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from agentguild_trustplane.cache import SignedDecisionCache
from agentguild_trustplane.client import GuildClient
from agentguild_trustplane.contract import (binding_violations, canonical_capability,
                                            request_capability_violation, validate_decision)
from agentguild_trustplane.gateway import Gateway
from agentguild_trustplane.policy import RiskPolicy

from test_client_transport_offline import FakeGuild, Issuer
from test_verifier_profile_offline import NOW

ENDPOINT = "https://worker.example/a2a"
ENDPOINT_SHA = "sha256:" + "ab" * 32


def routable_envelope(iss: Issuer, capability: str, *, envelope_capability=None,
                      drop_capability: bool = False, issuer_field: bool = True) -> dict:
    """A conformant (validate_decision == []) and routable (binding_violations
    == []) signed AgentGuildDecision, shaped like the Guild's /check?signed=true."""
    decision = {
        "contract": "AGD-1/1.0", "agent_id": "agent_worker",
        "identity": {"did": "did:key:zWorker", "did_method": "key", "custodial": True,
                     "did_control_proven": True, "first_party": False},
        "capability_match": {"requested": capability, "match": "exact",
                             "agent_capabilities": [capability]},
        "estimate": 0.8, "confidence": 0.7, "staleness": {"age_days": 1, "label": "fresh"},
        "has_declared_endpoint": True, "reachability_status": "reachable",
        "endpoint": ENDPOINT, "endpoint_sha256": ENDPOINT_SHA,
        "value_at_risk": {"tiers": {"micro": True, "low": True, "medium": True, "high": False},
                          "max_supported_tier": "medium", "basis": "evidence"},
        "evidence_provenance": {"counts": {"verifiable_outcome": 3}, "strongest": "verifiable_outcome",
                                "verifiable_collaborations": 3, "rules_version": "r1",
                                "checkpoint": {"index": 1}},
        "policy": {"result": None, "decided_by": "caller"},
    }
    env = {
        "type": "AgentGuildDecision", "contract": "AGD-1/1.0",
        "capability": capability if envelope_capability is None else envelope_capability,
        "status": "ok",
        "issued_at": (NOW - timedelta(seconds=5)).isoformat(),
        "valid_until": (NOW + timedelta(hours=1)).isoformat(),
        "decision": decision,
        "routing": {"routable": True, "provider_id": "agent_worker", "provider_did": "did:key:zWorker",
                    "endpoint": ENDPOINT, "endpoint_sha256": ENDPOINT_SHA},
    }
    if issuer_field:
        env["issuer"] = iss.did
    if drop_capability:
        env.pop("capability")
    return iss.sign(env)


def test_positive_control_is_conformant_and_routable():
    iss = Issuer()
    env = routable_envelope(iss, "code-review")
    assert validate_decision(env["decision"]) == []
    assert binding_violations(env) == []
    assert request_capability_violation("code-review", env) is None
    # the Guild canonicalises the request the same way before issuing
    assert request_capability_violation("Code Review", routable_envelope(iss, "code-review")) is None
    assert canonical_capability("  Code   Review ") == "code-review"
    assert canonical_capability("") is None and canonical_capability(None) is None


@pytest.mark.parametrize("requested,envelope_cap,reason", [
    ("code-review", "translation", "!= requested"),
    ("code-review", None, "no capability"),            # dropped
    ("code-review", "", "no capability"),
    ("code-review", 7, "no capability"),
    ("code-review", ["code-review"], "no capability"),
    ("code_review", "code-review", "!= requested"),   # same sanitised cache path, different id
    ("", "code-review", "not a valid capability id"),
])
def test_helper_rejects_missing_malformed_or_mismatched(requested, envelope_cap, reason):
    iss = Issuer()
    env = (routable_envelope(iss, "translation", drop_capability=True) if envelope_cap is None
           else routable_envelope(iss, "code-review", envelope_capability=envelope_cap))
    assert reason in (request_capability_violation(requested, env) or "")


# --- live path --------------------------------------------------------------------------
def test_live_mismatched_capability_is_unverified_and_never_pins():
    iss = Issuer()
    g = FakeGuild()
    try:
        c = GuildClient(g.base)
        g.json("/check", 200, routable_envelope(iss, "translation"))
        doc, channel, _ = c.signed_decision("code-review")
        assert doc is None and channel == "unverified"                 # was: live
        assert "request binding violated" in c.last_verify_failure
        assert c._local_pins == [] and c.stats["live_verify_failures"] == 1
        g.json("/check", 200, routable_envelope(iss, "code-review", drop_capability=True))
        doc, channel, _ = c.signed_decision("code-review")
        assert doc is None and channel == "unverified" and c._local_pins == []
        # positive control: the matching envelope is live, and only now pins
        g.json("/check", 200, routable_envelope(iss, "code-review"))
        doc, channel, age = c.signed_decision("code-review")
        assert doc is not None and channel == "live" and age == 0.0
        assert c._local_pins == [iss.did]
        # canonicalised request matches too (the Guild would issue for the canonical id)
        assert c.signed_decision("Code Review")[1] == "live"
        # a historical envelope without an issuer field still verifies and binds
        g.json("/check", 200, routable_envelope(iss, "code-review", issuer_field=False))
        assert c.signed_decision("code-review")[1] == "live"
    finally:
        g.close()


# --- cache path ------------------------------------------------------------------------------
def test_cache_put_and_get_bind_the_requested_capability(tmp_path):
    iss = Issuer()
    cache = SignedDecisionCache(tmp_path / "c")
    translation = routable_envelope(iss, "translation")
    assert cache.put("decision", "code-review", translation) is False   # was: True
    assert cache.trusted_issuers == []
    assert cache.put("decision", "translation", translation) is True
    assert cache.trusted_issuers == [iss.did]
    assert cache.get("decision", "translation")[1] == "fresh"
    # a wrong-capability entry written behind the cache's back is corrupt, not served
    cache._path("decision", "code-review").write_text(json.dumps({"stored_at": 0, "doc": translation}))
    assert cache.get("decision", "code-review") == (None, "corrupt", None)
    # keys whose sanitised paths collide share a slot: the envelope decides
    assert cache._path("decision", "code review") == cache._path("decision", "code_review")
    cache._path("decision", "code_review").write_text(
        json.dumps({"stored_at": 0, "doc": routable_envelope(iss, "code-review")}))
    assert cache.get("decision", "code review")[1] == "fresh"       # canonical 'code-review'
    assert cache.get("decision", "code_review") == (None, "corrupt", None)
    # expired-but-matching decisions keep the stale contract for the engine
    stale = iss.sign({**{k: v for k, v in routable_envelope(iss, "code-review").items() if k != "proof"},
                      "issued_at": (NOW - timedelta(days=3)).isoformat(),
                      "valid_until": (NOW - timedelta(days=1)).isoformat()})
    cache._path("decision", "code-review").write_text(json.dumps({"stored_at": 0, "doc": stale}))
    doc, state, age = cache.get("decision", "code-review")
    assert doc is not None and state == "stale" and age > 0


def test_cached_wrong_capability_does_not_pin_and_is_unverified_not_outage(tmp_path):
    iss = Issuer()
    cache = SignedDecisionCache(tmp_path / "c")
    cache._path("decision", "code-review").write_text(
        json.dumps({"stored_at": 0, "doc": routable_envelope(iss, "translation")}))
    dead = GuildClient("http://127.0.0.1:9", cache=cache, timeout=0.5)
    doc, channel, _ = dead.signed_decision("code-review")
    assert doc is None and channel == "unverified"                     # was: cache / translation
    assert "request binding" in dead.last_verify_failure
    assert cache.trusted_issuers == [] and dead._local_pins == []
    # enforce mode therefore fails CLOSED at a fail-open tier, not open as an outage
    gw = Gateway(policy=RiskPolicy(), state_dir=tmp_path / "gw")
    gw.cache._path("decision", "code-review").write_text(
        json.dumps({"stored_at": 0, "doc": routable_envelope(iss, "translation")}))
    gw.client.base = "http://127.0.0.1:9"
    gw.client.timeout = 0.5
    gate = gw.gate("code-review", value_at_risk=5.0)                 # micro tier fails open on outage
    assert gate.allowed is False and gate.policy.fail_state == "unverified"
    assert gate.channel == "unverified"
    # valid matching cached fallback is still used
    good = routable_envelope(iss, "code-review")
    assert cache.put("decision", "code-review", good)
    doc, channel, age = dead.signed_decision("code-review")
    assert doc == good and channel == "cache" and age is not None
    # a genuine miss remains an outage
    assert GuildClient("http://127.0.0.1:9", cache=cache, timeout=0.5).signed_decision("other")[1] == "outage"
