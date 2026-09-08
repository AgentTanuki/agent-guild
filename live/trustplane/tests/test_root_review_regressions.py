"""Regressions for the defects Root reproduced on f74f1213
(PYTHON-PACKAGE-ROOT-REVIEW-REPRO.json). Offline, synthetic keys only.

1. Guild-local passport lookups fell through as accepted when the credential
   id was absent / arbitrary / ``urn:passport:`` / ``urn:passport:<id>``
   without a timestamp — all binding to another subject — and pinned.
2. ``_cached_passport`` returned expired passports (ok=True, state=stale).
3. ``cache.get`` pinned the issuer of a wrong-subject or expired cached
   passport before the caller checked the subject.
4. ``verify_data_integrity`` treated a present malformed ``issuer``
   (None, 0, [], {}, {"id": 0}) as absent and verified true.
5. ``PAYMENT-REQUIRED`` header lookup was case-sensitive.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from agentguild_trustplane import verify as V
from agentguild_trustplane.cache import SignedDecisionCache
from agentguild_trustplane.client import GuildClient, PaymentRequired
from agentguild_trustplane.contract import passport_binding_violation

from test_client_transport_offline import FakeGuild, Issuer, fixture
from test_verifier_profile_offline import NOW, vc


# --- 1. local-id binding must be positively proven ------------------------------------
@pytest.mark.parametrize("cred_id", [
    None, "arbitrary", "urn:passport:", "urn:passport:agent_requested",
    "urn:passport:agent_requested:", "urn:passport:agent_requested:notanumber",
    "urn:passport:agent_other:1700000000", "URN:passport:agent_requested:1700000000",
    "urn:passport:agent_requested:1700000000:extra", 42, ["urn:passport:agent_requested:1"],
])
def test_local_id_lookup_rejects_unprovable_credential_ids(cred_id):
    iss = Issuer()
    kwargs = {"id": cred_id} if cred_id is not None else {}
    doc = vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1),
             subject="did:key:zSomeoneElse", **kwargs)
    if cred_id is None:
        doc = {k: v for k, v in doc.items() if k != "id"}
        doc = iss.sign({k: v for k, v in doc.items() if k != "proof"})
    assert passport_binding_violation("agent_requested", doc) is not None
    # end-to-end: live lookup rejected and NOT pinned
    g = FakeGuild()
    try:
        g.json("/agents/", 200, doc)
        c = GuildClient(g.base)
        r = c.passport_result("agent_requested")
        assert not r.ok and r.channel == "unverified" and c._local_pins == []
    finally:
        g.close()


def test_local_id_binding_positive_and_did_binding_unchanged():
    iss = Issuer()
    good = vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1),
              id="urn:passport:agent_requested:1700000000")
    assert passport_binding_violation("agent_requested", good) is None
    assert passport_binding_violation("did:key:zSubject", good) is None
    assert "subject mismatch" in passport_binding_violation("did:key:zX", good)
    live = fixture("live_passport_2026-09-08.json")["body"]
    assert passport_binding_violation("agent_d0a8f6ef9b41", live) is None
    assert passport_binding_violation(live["credentialSubject"]["id"], live) is None


@pytest.mark.parametrize("subject_id", [None, "", 0, [], {}])
def test_credential_subject_id_must_be_nonempty_string(subject_id):
    iss = Issuer()
    base = {k: v for k, v in vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)).items() if k != "proof"}
    base["credentialSubject"] = {"trust": 1.0} if subject_id is None else {"id": subject_id, "trust": 1.0}
    doc = iss.sign(base)
    assert passport_binding_violation("agent_1", doc) is not None
    assert passport_binding_violation("did:key:zSubject", doc) is not None
    assert passport_binding_violation("", doc) is not None


# --- 2 + 3. cached passports: fresh, bound, and never a pin source --------------------
def test_cached_expired_passport_is_not_returned_and_does_not_pin(tmp_path):
    iss = Issuer()
    cache = SignedDecisionCache(tmp_path / "c")
    expired = vc(iss, start=NOW - timedelta(days=9), end=NOW - timedelta(days=2))
    cache._path("passport", "did:key:zSubject").write_text(json.dumps({"stored_at": 0, "doc": expired}))
    assert cache.get("passport", "did:key:zSubject")[0] is None
    assert cache.trusted_issuers == []                                   # was pinned before
    dead = GuildClient("http://127.0.0.1:9", cache=cache, timeout=0.5)
    r = dead.passport_result("did:key:zSubject")
    assert not r.ok and r.channel == "outage" and "not usable" in r.reason   # was ok=True/stale
    assert dead.passport("did:key:zSubject") is None
    assert cache.trusted_issuers == []


def test_cached_wrong_subject_passport_is_rejected_before_any_pin(tmp_path):
    iss = Issuer()
    cache = SignedDecisionCache(tmp_path / "c")
    other = vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1), subject="did:key:zOther")
    cache._path("passport", "did:key:zSubject").write_text(json.dumps({"stored_at": 0, "doc": other}))
    assert cache.get("passport", "did:key:zSubject") == (None, "corrupt", None)
    assert cache.trusted_issuers == []                                   # was pinned before
    dead = GuildClient("http://127.0.0.1:9", cache=cache, timeout=0.5)
    assert not dead.passport_result("did:key:zSubject").ok
    assert cache.trusted_issuers == []
    # a later legitimate issuer still gets the first pin
    assert cache.put("passport", "did:key:zOther", other) and cache.trusted_issuers == [iss.did]


def test_cache_put_refuses_passport_under_wrong_key(tmp_path):
    iss = Issuer()
    cache = SignedDecisionCache(tmp_path / "c")
    doc = vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1))
    assert cache.put("passport", "agent_9", doc) is False and cache.trusted_issuers == []
    assert cache.put("passport", "agent_1", doc) is True                 # urn:passport:agent_1:1
    assert cache.get("passport", "agent_1")[1] == "fresh"


def test_live_then_cache_roundtrip_stays_fresh_only(tmp_path):
    iss = Issuer()
    cache = SignedDecisionCache(tmp_path / "c")
    g = FakeGuild()
    try:
        g.json("/agents/", 200, vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)))
        live = GuildClient(g.base, cache=cache)
        assert live.passport_result("did:key:zSubject").channel == "live"
    finally:
        g.close()
    dead = GuildClient("http://127.0.0.1:9", cache=cache, timeout=0.5)
    r = dead.passport_result("did:key:zSubject")
    assert r.ok and r.channel == "cache" and r.state == "fresh"


# --- 4. malformed issuer ------------------------------------------------------------------
@pytest.mark.parametrize("issuer", [None, 0, [], {}, {"id": 0}, "", {"id": ""}, ["did:key:z"]])
def test_present_malformed_issuer_is_rejected(issuer):
    iss = Issuer()
    base = {k: v for k, v in vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)).items() if k != "proof"}
    base["issuer"] = issuer                       # present, malformed (None included)
    v = V.verify_data_integrity(iss.sign(base))
    assert v["verified"] is False and "malformed" in v["reason"]


def test_credential_without_issuer_is_rejected_but_decision_envelope_may_omit():
    iss = Issuer()
    base = {k: v for k, v in vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)).items()
            if k not in ("proof", "issuer")}
    v = V.verify_data_integrity(iss.sign(base))
    assert v["verified"] is False and "no issuer" in v["reason"]
    env = iss.sign({"type": "AgentGuildDecision", "issued_at": (NOW - timedelta(seconds=5)).isoformat(),
                    "valid_until": (NOW + timedelta(hours=1)).isoformat(), "decision": {}})
    assert V.verify_data_integrity(env)["verified"]
    # object-form issuer with a well-formed id still verifies
    obj = vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1), issuer_field={"id": iss.did})
    assert V.verify_data_integrity(obj)["verified"]


# --- 5. header casing ---------------------------------------------------------------------
@pytest.mark.parametrize("header_name", ["PAYMENT-REQUIRED", "payment-required", "Payment-Required", "pAyMeNt-ReQuIrEd"])
def test_payment_required_header_lookup_is_case_insensitive(header_name):
    fx = fixture("live_check_402_2026-09-08.json")
    g = FakeGuild()
    try:
        g.json("/check", 402, {"detail": "pay"}, {header_name: fx["headers"]["payment-required"]})
        c = GuildClient(g.base)
        with pytest.raises(PaymentRequired) as ei:
            c._get("/check?capability=x")
        assert ei.value.payment_required_header == fx["headers"]["payment-required"]
        assert ei.value.terms[0]["amount"] == "1000000"       # terms from the header, body had none
    finally:
        g.close()


# --- 6. quote()/preflight() are strictly non-spending ---------------------------------------
CREDENTIALED = {
    "Authorization": "Bearer secret", "x-api-key": "HDR-KEY", "PAYMENT-SIGNATURE": "sig",
    "X-Payment": "v1", "Cookie": "session=abc", "x-payment-signature": "sig2",
    "Proxy-Authorization": "Basic x", "X-Admin-Token": "admin",
}
ATTRIBUTION = {"X-Agent-Guild-First-Party": "tok", "X-Agent-Guild-Role": "test", "X-Guild-Source": "lab"}


def _credential_headers_seen(reqs):
    from agentguild_trustplane.client import CREDENTIAL_HEADERS
    return sorted({k for _, _, hdrs, _ in reqs for k in hdrs if k.lower() in CREDENTIAL_HEADERS})


def test_quote_sends_no_credentials_even_on_an_authenticated_client():
    fx = fixture("live_check_402_2026-09-08.json")
    g = FakeGuild()
    try:
        g.json("/check", 402, fx["body"], {"PAYMENT-REQUIRED": fx["headers"]["payment-required"]})
        c = GuildClient(g.base, api_key="OFFLINE-TEST-KEY", extra_headers={**CREDENTIALED, **ATTRIBUTION})
        q = c.quote("code-review")
        assert q["status"] == "payment_required" and q["terms"][0]["amount"] == "1000000"
        assert len(g.requests) == 1                                   # no retry
        hdrs = g.requests[0][2]
        assert _credential_headers_seen(g.requests) == []             # was: x-api-key sent
        assert "OFFLINE-TEST-KEY" not in json.dumps(hdrs) and "secret" not in json.dumps(hdrs)
        for k, v in ATTRIBUTION.items():                              # attribution preserved
            assert hdrs[k.lower()] == v
        assert c.api_key == "OFFLINE-TEST-KEY"                        # credentials not mutated
        assert c.extra_headers["Authorization"] == "Bearer secret"
        # a served answer is returned unverified, with no second request
        g.json("/check", 200, {"decision": {"contract": "AGD-1/1.0"}})
        q = c.quote("code-review")
        assert q["status"] == "served" and q["verified"] is False and "verify" in q["note"]
        assert len(g.requests) == 2 and _credential_headers_seen(g.requests) == []
    finally:
        g.close()


def test_preflight_sends_no_credentials_even_on_an_authenticated_client():
    g = FakeGuild()
    try:
        g.json("/preflight", 200, {"target": "x", "verdict": "no_failed_checks"})
        c = GuildClient(g.base, api_key="OFFLINE-TEST-KEY", extra_headers={**CREDENTIALED, **ATTRIBUTION})
        assert c.preflight("https://example.com").verdict == "no_failed_checks"
        assert _credential_headers_seen(g.requests) == []
        assert g.requests[0][2]["x-agent-guild-first-party"] == "tok"
    finally:
        g.close()


def test_authenticated_signed_decision_still_sends_the_key():
    """Existing behaviour, kept and documented: an explicit signed_decision on
    an authenticated client presents the key (and may be metered)."""
    g = FakeGuild()
    try:
        g.json("/check", 200, {"not": "signed"})
        c = GuildClient(g.base, api_key="OFFLINE-TEST-KEY")
        c.signed_decision("x")
        assert g.requests[0][2]["x-api-key"] == "OFFLINE-TEST-KEY"
    finally:
        g.close()
