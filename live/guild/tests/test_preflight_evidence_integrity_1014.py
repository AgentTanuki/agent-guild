"""Regression suite for the 14 October 2026 assessment of the preflight
evidence path. Six findings, each reproduced on main @ 1abda0c before the
fix and pinned here as the counterexample that must stay red-if-broken:

  F1  a registered DID merely NAMED in a card (zero attestations, nothing
      binding the endpoint to the identity) was `independent_evidence: proven`;
  F2  `agent_card_signed: proven` meant "a signatures member exists" — no
      verification, presence read as a pass;
  F3  a skip after a warning about check X, with a direct-check failure of
      unrelated Y, was credited as `avoided_broken_endpoint`;
  F4  a registered key on both legs flipped `independently_demonstrated_benefit`
      — identity continuity read as operator independence;
  F5  the MCP preflight / outcome tools could not carry the participant
      attribution the HTTP route supports;
  F6  invalid ports, malformed IPv6 literals and non-object JSON cards
      raised out of the probe and surfaced as HTTP 500.

Passing here proves these specific misreadings are gone. It proves nothing
about usefulness, safety, or any endpoint.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os

import pytest

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from fastmcp import Client  # noqa: E402

from app import a2a_card_signing, abuse, crypto, preflight, reachability  # noqa: E402
from app import preflight_outcomes as po  # noqa: E402
from app import main as main_module  # noqa: E402
from app import mcp_server as mcp_module  # noqa: E402
from app.main import app  # noqa: E402
from app.mcp_server import mcp as guild_mcp  # noqa: E402
from app.store import Store  # noqa: E402

client = TestClient(app)
UA = "python-httpxish/1.0"
OK_PROBE = {"status": "recently_reachable", "evidence_level": "protocol_handshake"}
TARGET = "https://agent.example/a2a"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _fake_net(monkeypatch, *, card: dict | bytes | None, extra: dict | None = None,
              probe=OK_PROBE, card_code=200):
    """Stub the network: the liveness probe, the agent card, and any other
    same-origin well-known document (`extra` maps path → (code, body))."""
    monkeypatch.setattr(reachability, "liveness_probe", lambda url, **kw: probe)
    body = (json.dumps(card).encode() if isinstance(card, dict)
            else (card or b""))

    def _get(url, path, timeout_ctx=None):
        if "agent-card" in path:
            return card_code, body, ""
        if extra and path in extra:
            code, b = extra[path]
            return code, (json.dumps(b).encode() if isinstance(b, dict) else b), ""
        return 404, b"", ""
    monkeypatch.setattr(preflight, "_probe_get", _get)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sign(card: dict, priv_hex: str, kid: str, alg: str = "EdDSA") -> dict:
    """A JWS AgentCardSignature over `card` naming `kid`, like a counterparty
    would produce (spec §8.4: JCS of the card minus `signatures`)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    header = {"alg": alg, "typ": "JOSE", "kid": kid}
    protected = _b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    signing_input = protected + "." + _b64(a2a_card_signing.canonical_payload(card))
    sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex)).sign(
        signing_input.encode("ascii"))
    out = {k: v for k, v in card.items() if k != "signatures"}
    out["signatures"] = [{"protected": protected, "signature": _b64(sig)}]
    return out


def _check(out: dict, name: str) -> dict:
    return next(c for c in out["checks"] if c["check"] == name)


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    abuse.reset()
    st = Store(path=str(tmp_path / "guild.json"))
    monkeypatch.setattr(main_module, "store", st)
    monkeypatch.setattr(mcp_module, "store", st)
    return st


def _registered(st: Store, **meta) -> dict:
    return st.register_agent("counterparty", ["code-review"], dict(meta))


# ==========================================================================
# F1 — a DID named in a card is a CLAIM until bound, and evidence needs evidence
# (tightened 19 Oct after independent review: a verified signature alone is
# not a binding — a signed card can be replayed from any origin — and only
# verified, non-self, non-first-party attestations count.)
# ==========================================================================
def _attest(st: Store, subject: dict, *, issuer: dict | None = None,
            verified: bool = True) -> dict:
    issuer = issuer or _registered(st)
    rec = {"id": "att_" + str(len(st.attestations)), "issuer_id": issuer["id"],
           "subject_id": subject["id"], "verified": verified}
    st.attestations.append(rec)
    return issuer


def _signed_card(agent: dict, **fields) -> dict:
    return _sign({"protocolVersion": "0.3", "did": agent["did"], **fields},
                 agent["private_key"], crypto.did_key_verification_method(agent["did"]))


def test_f1_named_registered_did_with_zero_attestations_is_unknown(monkeypatch, fresh):
    agent = _registered(fresh)
    _fake_net(monkeypatch, card={"protocolVersion": "0.3", "did": agent["did"]})
    ev = _check(preflight.run(TARGET, store=fresh), "independent_evidence")
    assert ev["status"] == "unknown"
    assert ev["identity_match"] == "claimed_unverified"
    assert ev["attestations_received"] == 0
    assert "agent_id" not in ev               # nothing bound; nothing to audit


def test_f1_bound_identity_without_attestations_is_still_unknown(monkeypatch, fresh):
    agent = _registered(fresh)
    _fake_net(monkeypatch, card=_signed_card(agent, url=TARGET))
    ev = _check(preflight.run(TARGET, store=fresh), "independent_evidence")
    assert ev["status"] == "unknown"
    assert ev["identity_match"] == "bound" and ev["binding"] == "card_signature_and_url"
    assert ev["qualifying_attestations"] == 0


def test_f1_signature_over_this_url_plus_qualifying_attestation_is_proven(monkeypatch, fresh):
    agent = _registered(fresh)
    _attest(fresh, agent)
    _fake_net(monkeypatch, card=_signed_card(agent, url=TARGET))
    ev = _check(preflight.run(TARGET, store=fresh), "independent_evidence")
    assert ev["status"] == "proven"
    assert ev["binding"] == "card_signature_and_url"
    assert ev["binding_strength"] == "cryptographic"
    assert ev["qualifying_attestations"] == 1 and ev["distinct_issuers"] == 1
    assert ev["agent_id"] == agent["id"]


def test_f1_replayed_signed_card_from_another_origin_does_not_bind(monkeypatch, fresh):
    """Review finding 1: the JWS covers the card bytes, not where they are
    served from. The real agent's card, copied verbatim to an impostor
    origin, must not bind the impostor endpoint to the agent."""
    agent = _registered(fresh)
    _attest(fresh, agent)
    real = _signed_card(agent, url="https://real.example/a2a")
    _fake_net(monkeypatch, card=real)
    assert _check(preflight.run("https://real.example/a2a", store=fresh),
                  "independent_evidence")["status"] == "proven"
    ev = _check(preflight.run("https://impostor.example/a2a", store=fresh),
                "independent_evidence")
    assert ev["status"] == "unknown" and ev["identity_match"] == "claimed_unverified"
    assert "different endpoint url" in ev["detail"]
    # a signed card that names no url at all binds nothing either
    _fake_net(monkeypatch, card=_signed_card(agent))
    ev = _check(preflight.run(TARGET, store=fresh), "independent_evidence")
    assert ev["status"] == "unknown" and ev["identity_match"] == "claimed_unverified"


def test_f1_registered_endpoint_declaration_binds_same_origin_and_path_only(monkeypatch, fresh):
    agent = _registered(fresh, endpoint="https://agent.example/a2a")
    _attest(fresh, agent)
    _fake_net(monkeypatch, card={"protocolVersion": "0.3", "did": agent["did"]})
    ev = _check(preflight.run(TARGET, store=fresh), "independent_evidence")
    assert ev["status"] == "proven"
    assert ev["binding"] == "registered_endpoint"
    assert ev["binding_strength"] == "self_declared_unverified"   # labelled as such
    # the same card served from ANOTHER origin is a claim, not a binding
    ev2 = _check(preflight.run("https://impostor.example/a2a", store=fresh),
                 "independent_evidence")
    assert ev2["status"] == "unknown" and ev2["identity_match"] == "claimed_unverified"


def test_f1_declaration_on_a_shared_host_does_not_cover_another_tenant(monkeypatch, fresh):
    """Review finding 5: path-multi-tenant hosts. A declaration at
    /tenant-a must not bind /tenant-evil on the same origin."""
    agent = _registered(fresh, endpoint="https://shared.example/tenant-a")
    _attest(fresh, agent)
    _fake_net(monkeypatch, card={"protocolVersion": "0.3", "did": agent["did"]})
    ok = _check(preflight.run("https://shared.example/tenant-a/a2a", store=fresh),
                "independent_evidence")
    assert ok["status"] == "proven"
    bad = _check(preflight.run("https://shared.example/tenant-evil", store=fresh),
                 "independent_evidence")
    assert bad["status"] == "unknown" and bad["identity_match"] == "claimed_unverified"
    root = _registered(fresh, endpoint="https://root.example/")
    _attest(fresh, root)
    _fake_net(monkeypatch, card={"protocolVersion": "0.3", "did": root["did"]})
    assert _check(preflight.run("https://root.example/anything/a2a", store=fresh),
                  "independent_evidence")["status"] == "proven"


def test_f1_unverified_self_and_first_party_attestations_never_make_proven(monkeypatch, fresh):
    """Review finding 4: registering a puppet and attesting to oneself costs
    nothing; an unverified credential is stored but proves nothing."""
    agent = _registered(fresh, endpoint=TARGET)
    _attest(fresh, agent, verified=False)                     # unverified
    _attest(fresh, agent, issuer=agent)                       # self-issued
    ours = fresh.register_agent("ours", ["x"], {}, first_party=True)
    _attest(fresh, agent, issuer=ours)                        # first-party issuer
    _fake_net(monkeypatch, card={"protocolVersion": "0.3", "did": agent["did"]})
    ev = _check(preflight.run(TARGET, store=fresh), "independent_evidence")
    assert ev["status"] == "unknown" and ev["identity_match"] == "bound"
    assert ev["attestations_received"] == 3 and ev["qualifying_attestations"] == 0
    assert "QUALIFYING" in ev["detail"]
    _attest(fresh, agent)                                     # one real one
    ev = _check(preflight.run(TARGET, store=fresh), "independent_evidence")
    assert ev["status"] == "proven" and ev["qualifying_attestations"] == 1
    assert ev["attestations_received"] == 4


def test_f1_signature_by_a_different_key_than_the_claimed_did_does_not_bind(monkeypatch, fresh):
    agent = _registered(fresh)
    _attest(fresh, agent)
    other_priv, other_pub = crypto.generate_keypair()
    other_did = crypto.did_from_public_key(other_pub)
    # signed by someone else, but the card names the registered agent's DID
    card = _sign({"protocolVersion": "0.3", "did": agent["did"], "url": TARGET},
                 other_priv, crypto.did_key_verification_method(other_did))
    _fake_net(monkeypatch, card=card)
    out = preflight.run(TARGET, store=fresh)
    assert _check(out, "agent_card_signed")["status"] == "proven"   # valid JWS...
    ev = _check(out, "independent_evidence")
    assert ev["status"] == "unknown"                                # ...wrong key
    assert ev["identity_match"] == "claimed_unverified"


# ==========================================================================
# F2 — presence, validity and binding of a card signature are three statements
# ==========================================================================
def test_f2_presence_without_a_resolvable_key_is_unknown_not_proven(monkeypatch):
    _fake_net(monkeypatch, card={"protocolVersion": "0.3",
                                 "signatures": [{"protected": "x", "signature": "y"}]})
    sig = _check(preflight.run(TARGET), "agent_card_signed")
    assert sig["status"] == "unknown"
    assert sig["verification"] == "unverified_presence"


def test_f2_valid_did_key_signature_is_verified(monkeypatch):
    priv, pub = crypto.generate_keypair()
    did = crypto.did_from_public_key(pub)
    card = _sign({"protocolVersion": "0.3", "name": "x"}, priv,
                 crypto.did_key_verification_method(did))
    _fake_net(monkeypatch, card=card)
    sig = _check(preflight.run(TARGET), "agent_card_signed")
    assert sig["status"] == "proven" and sig["verification"] == "verified"
    assert sig["key_source"].startswith("did:key")
    assert sig["signer_did_key"] == did


def test_f2_tampered_card_with_a_valid_key_is_failed_not_proven(monkeypatch):
    priv, pub = crypto.generate_keypair()
    did = crypto.did_from_public_key(pub)
    card = _sign({"protocolVersion": "0.3", "name": "x"}, priv,
                 crypto.did_key_verification_method(did))
    card["name"] = "tampered after signing"
    _fake_net(monkeypatch, card=card)
    sig = _check(preflight.run(TARGET), "agent_card_signed")
    assert sig["status"] == "failed" and sig["verification"] == "invalid"
    assert preflight.run(TARGET)["verdict"] == "delegate_with_caution"


def test_f2_same_origin_did_web_key_is_resolved_and_verified(monkeypatch):
    priv, pub = crypto.generate_keypair()
    identity = {"public_key": pub, "private_key": priv,
                "did": crypto.did_from_public_key(pub)}
    origin = "https://agent.example"
    from app import x402_artifacts
    did_doc = x402_artifacts.did_web_document(identity, origin)
    card = a2a_card_signing.sign_agent_card({"protocolVersion": "0.3"}, identity, origin)
    _fake_net(monkeypatch, card=card, extra={"/.well-known/did.json": (200, did_doc)})
    sig = _check(preflight.run(TARGET), "agent_card_signed")
    assert sig["status"] == "proven" and sig["key_source"] == "did:web (same origin)"


def test_f2_cross_origin_did_web_key_is_never_fetched(monkeypatch):
    priv, pub = crypto.generate_keypair()
    identity = {"public_key": pub, "private_key": priv,
                "did": crypto.did_from_public_key(pub)}
    card = a2a_card_signing.sign_agent_card({"protocolVersion": "0.3"}, identity,
                                            "https://elsewhere.example")
    fetched: list[str] = []
    _fake_net(monkeypatch, card=card)
    orig = preflight._probe_get

    def spy(url, path, timeout_ctx=None):
        fetched.append(path)
        return orig(url, path, timeout_ctx)
    monkeypatch.setattr(preflight, "_probe_get", spy)
    sig = _check(preflight.run(TARGET), "agent_card_signed")
    assert sig["status"] == "unknown" and sig["verification"] == "unverified_presence"
    assert "cross-origin" in sig["detail"]
    assert "/.well-known/did.json" not in fetched


def test_f2_unsupported_alg_is_unknown(monkeypatch):
    priv, pub = crypto.generate_keypair()
    card = _sign({"protocolVersion": "0.3"}, priv,
                 crypto.did_key_verification_method(crypto.did_from_public_key(pub)),
                 alg="ES256")
    _fake_net(monkeypatch, card=card)
    sig = _check(preflight.run(TARGET), "agent_card_signed")
    assert sig["status"] == "unknown" and "ES256" in sig["detail"]


def test_f2_hostile_key_material_never_escapes_the_verifier(monkeypatch):
    """Review finding 2: `did:key:z`, non-list verificationMethod / keys."""
    priv, pub = crypto.generate_keypair()
    for card, extra in (
        ({"protocolVersion": "0.3", "did": "did:key:z",
          "signatures": [{"protected": _b64(json.dumps({"alg": "EdDSA", "kid": "did:key:z"}).encode()),
                          "signature": "x"}]}, None),
        (a2a_card_signing.sign_agent_card({"protocolVersion": "0.3"},
                                          {"public_key": pub, "private_key": priv,
                                           "did": crypto.did_from_public_key(pub)},
                                          "https://agent.example"),
         {"/.well-known/did.json": (200, {"verificationMethod": 5})}),
        (_sign({"protocolVersion": "0.3"}, priv, "urn:some-kid"),
         {"/.well-known/jwks.json": (200, {"keys": 5})}),
    ):
        _fake_net(monkeypatch, card=card, extra=extra)
        out = preflight.run(TARGET)
        assert "input_error" not in out, out
        sig = _check(out, "agent_card_signed")
        assert sig["status"] == "unknown" and sig["verification"] == "unverified_presence"
        assert _check(out, "endpoint_reachable")["status"] == "proven"


def test_f2_did_web_over_plain_http_is_not_resolved(monkeypatch):
    """Review finding 6: did:web resolves over https only."""
    priv, pub = crypto.generate_keypair()
    identity = {"public_key": pub, "private_key": priv,
                "did": crypto.did_from_public_key(pub)}
    from app import x402_artifacts
    did_doc = x402_artifacts.did_web_document(identity, "http://agent.example")
    card = a2a_card_signing.sign_agent_card({"protocolVersion": "0.3"}, identity,
                                            "http://agent.example")
    _fake_net(monkeypatch, card=card, extra={"/.well-known/did.json": (200, did_doc)})
    sig = _check(preflight.run("http://agent.example/a2a"), "agent_card_signed")
    assert sig["status"] == "unknown" and "https only" in sig["detail"]


def test_f2_a_later_valid_signature_still_verifies_after_an_invalid_one(monkeypatch):
    """Review note 7: multi-signature cards."""
    priv, pub = crypto.generate_keypair()
    did = crypto.did_from_public_key(pub)
    good = _sign({"protocolVersion": "0.3"}, priv, crypto.did_key_verification_method(did))
    bad_entry = dict(good["signatures"][0], signature=_b64(b"\x00" * 64))
    card = dict(good, signatures=[bad_entry, good["signatures"][0]])
    _fake_net(monkeypatch, card=card)
    sig = _check(preflight.run(TARGET), "agent_card_signed")
    assert sig["status"] == "proven" and sig["invalid_entries"] == 1


def test_f2_data_integrity_proof_is_presence_only(monkeypatch):
    _fake_net(monkeypatch, card={"protocolVersion": "0.3", "proof": {"type": "x"}})
    sig = _check(preflight.run(TARGET), "agent_card_signed")
    assert sig["status"] == "unknown" and sig["verification"] == "unverified_presence"


def test_f2_limits_text_states_the_verification_rule(monkeypatch):
    _fake_net(monkeypatch, card={"protocolVersion": "0.3"})
    out = preflight.run(TARGET)
    assert "VERIFIED only when" in out["limits"]
    assert "presence of a card signature, not its validity" not in out["limits"]


# ==========================================================================
# F3 — the observed failure must match the specific warning
# ==========================================================================
def _run(verdict, failed, unknown, scored):
    return {"verdict": verdict, "failed_checks": failed, "unknown_checks": unknown,
            "scored_checks": scored}


ALL = ["endpoint_reachable", "protocol_handshake", "agent_card_resolves",
       "agent_card_signed", "payment_claim_holds", "independent_evidence"]
UNSIGNED = _run("delegate_with_caution", ["agent_card_signed"],
                ["independent_evidence"],
                [c for c in ALL if c != "independent_evidence"])
DOWN = _run("do_not_delegate", ["protocol_handshake"], ["independent_evidence"],
            [c for c in ALL if c != "independent_evidence"])
CLEAN = _run("no_failed_checks", [], ["independent_evidence"],
             [c for c in ALL if c != "independent_evidence"])


def _rec(detail=None, basis="ag_evidence", action="declined", direct="failure"):
    body = {"preflight_id": "pf_" + "a" * 20, "action": action,
            "baseline": {"direct_check": direct}, "decision_basis": basis,
            "reporter_kind": "agent"}
    if detail is not None:
        body["baseline"]["detail"] = detail
    return po.validate(body)


def test_f3_unsigned_card_warning_cannot_predict_a_wrong_result():
    c = po.classify(UNSIGNED, _rec("wrong_result"))
    assert c == {"result": "failure_out_of_scope", "kind": "unresolved"}


def test_f3_unsigned_card_warning_cannot_predict_a_timeout_either():
    # reachability was PROVEN; a direct-check timeout is AG's missed problem
    c = po.classify(UNSIGNED, _rec("timeout"))
    assert c == {"result": "missed_problem", "kind": "mistake"}


def test_f3_matching_failure_is_the_only_route_to_benefit():
    assert po.classify(UNSIGNED, _rec("signature_invalid")) == {
        "result": "avoided_broken_endpoint", "kind": "benefit"}
    assert po.classify(DOWN, _rec("protocol_error")) == {
        "result": "avoided_broken_endpoint", "kind": "benefit"}
    assert po.classify(DOWN, _rec("protocol_error", basis="own_check")) == {
        "result": "correct_warning_independently_confirmed", "kind": "neutral"}
    assert po.classify(DOWN, _rec("protocol_error", basis="unknown")) == {
        "result": "correct_warning_influence_unknown", "kind": "unresolved"}


def test_f3_direct_failure_without_a_detail_is_unresolved_never_benefit():
    assert po.classify(DOWN, _rec(None)) == {
        "result": "direct_check_unspecified", "kind": "unresolved"}
    assert po.classify(DOWN, _rec("not_applicable")) == {
        "result": "direct_check_unspecified", "kind": "unresolved"}


def test_f3_failure_in_a_check_ag_reported_unknown_is_not_covered():
    assert po.classify(DOWN, _rec("identity_mismatch")) == {
        "result": "unknown_check_not_covered", "kind": "neutral"}


def test_f3_clean_verdict_is_a_missed_problem_only_for_a_check_ag_passed():
    assert po.classify(CLEAN, _rec("protocol_error", basis="own_check")) == {
        "result": "missed_problem", "kind": "mistake"}
    assert po.classify(CLEAN, _rec("wrong_result", basis="own_check")) == {
        "result": "failure_out_of_scope", "kind": "unresolved"}


def test_f3_baseline_detail_is_accepted_over_http_and_validated(fresh, monkeypatch):
    def fake_run(url, *, store=None):
        return {"target": url, "verdict": "delegate_with_caution", "headline": "x",
                "checks": [], "failed": ["agent_card_signed"],
                "unknowns": ["independent_evidence"],
                "scored": [c for c in ALL if c != "independent_evidence"],
                "method": "m", "limits": "l"}
    monkeypatch.setattr(preflight, "run", fake_run)
    pf = client.get("/preflight", params={"url": "https://x.example/a"},
                    headers={"User-Agent": UA}).json()
    r = client.post("/preflight/outcome", headers={"User-Agent": UA}, json={
        "preflight_id": pf["preflight_id"], "action": "declined",
        "baseline": {"direct_check": "failure", "detail": "wrong_result"},
        "decision_basis": "ag_evidence", "reporter_kind": "agent"})
    assert r.status_code == 201
    b = client.get("/preflight/outcomes").json()["by_ownership"]["heuristic_external"]
    assert b["by_result"] == {"failure_out_of_scope": 1} and b["benefit"] == 0
    bad = client.post("/preflight/outcome", headers={"User-Agent": UA}, json={
        "preflight_id": pf["preflight_id"], "action": "declined",
        "baseline": {"direct_check": "failure", "detail": "exploded"},
        "reporter_kind": "agent"})
    assert bad.status_code == 422 and "baseline.detail" in bad.text


def test_f3_rules_table_states_the_matching_requirement():
    rules = {r["result"]: r for r in po.RULES}
    assert "detail maps to a check AG reported FAILED" in rules["avoided_broken_endpoint"]["when"]
    assert "direct_check_unspecified" in rules


# ==========================================================================
# F4 — a registered key is identity continuity, not independence
# ==========================================================================
def _pf_and_report(monkeypatch, key: str, detail="protocol_error"):
    def fake_run(url, *, store=None):
        return {"target": url, "verdict": "do_not_delegate", "headline": "x",
                "checks": [], "failed": ["protocol_handshake"],
                "unknowns": ["independent_evidence"],
                "scored": [c for c in ALL if c != "independent_evidence"],
                "method": "m", "limits": "l"}
    monkeypatch.setattr(preflight, "run", fake_run)
    h = {"User-Agent": UA, "X-API-Key": key}
    pf = client.get("/preflight", params={"url": "https://x.example/a"}, headers=h).json()
    r = client.post("/preflight/outcome", headers=h, json={
        "preflight_id": pf["preflight_id"], "action": "declined",
        "baseline": {"direct_check": "failure", "detail": detail},
        "decision_basis": "ag_evidence", "reporter_kind": "integration"})
    assert r.status_code == 201, r.text
    return pf


def test_f4_registered_key_benefit_does_not_flip_the_headline(fresh, monkeypatch):
    agent = _registered(fresh)
    _pf_and_report(monkeypatch, agent["api_key"])
    s = client.get("/preflight/outcomes").json()
    assert s["by_ownership"]["registered_participant"]["benefit"] == 1
    assert s["independently_demonstrated_benefit"] is False
    assert s["independent"]["attested_participants"] == 0
    assert s["independent"]["benefit"] == 0
    assert "identity continuity" in s["headline"]
    assert "identity continuity" in s["reading_rules"]["registered_participant"].lower() \
        or "IDENTITY CONTINUITY" in s["reading_rules"]["registered_participant"]


def test_f4_attested_independence_is_what_moves_the_headline_and_is_revocable(
        fresh, monkeypatch):
    monkeypatch.setattr(main_module, "ADMIN_TOKEN", "t0k")
    agent = _registered(fresh)
    _pf_and_report(monkeypatch, agent["api_key"])
    admin = {"X-Admin-Token": "t0k"}
    assert client.post("/admin/evaluation/participants", json={
        "agent_id": agent["id"]}, headers=admin).status_code == 422   # basis required
    assert client.post("/admin/evaluation/participants", json={
        "agent_id": agent["id"], "basis": "operator_outside_project_team"},
        headers=admin).status_code == 422                            # source required
    r = client.post("/admin/evaluation/participants", json={
        "agent_id": agent["id"], "basis": "operator_outside_project_team",
        "public_source": "https://example.org/operator-thread"}, headers=admin)
    assert r.status_code == 201
    s = client.get("/preflight/outcomes").json()
    assert s["independently_demonstrated_benefit"] is True
    assert s["independent"]["benefit"] == 1
    assert "attested-independent" in s["headline"]
    assert "a claim" in s["headline"]          # corroboration is still a claim
    listing = client.get("/admin/evaluation/participants", headers=admin).json()
    assert agent["id"] in listing["attested"]
    # revoke → headline reverts; nothing else about the records changes
    r = client.post("/admin/evaluation/participants", json={
        "agent_id": agent["id"], "revoke": True}, headers=admin)
    assert r.status_code == 201 and r.json()["attested"] is False
    s = client.get("/preflight/outcomes").json()
    assert s["independently_demonstrated_benefit"] is False
    assert s["by_ownership"]["registered_participant"]["benefit"] == 1


def test_f4_attestation_is_operator_only_and_never_first_party(fresh, monkeypatch):
    monkeypatch.setattr(main_module, "ADMIN_TOKEN", "t0k")
    agent = _registered(fresh)
    body = {"agent_id": agent["id"], "basis": "operator_outside_project_team",
            "public_source": "https://example.org/x"}
    assert client.post("/admin/evaluation/participants", json=body).status_code == 403
    assert client.post("/admin/evaluation/participants", json=body,
                       headers={"X-Admin-Token": "wrong"}).status_code == 403
    fp = fresh.register_agent("ours", ["x"], {}, first_party=True)
    r = client.post("/admin/evaluation/participants",
                    json={**body, "agent_id": fp["id"]},
                    headers={"X-Admin-Token": "t0k"})
    assert r.status_code == 409
    assert client.post("/admin/evaluation/participants",
                       json={**body, "agent_id": "agent_nope"},
                       headers={"X-Admin-Token": "t0k"}).status_code == 404


def test_f4_unattested_benefit_from_attested_participant_requires_same_identity(
        fresh, monkeypatch):
    """Attesting agent A does not promote decisions made under key B."""
    monkeypatch.setattr(main_module, "ADMIN_TOKEN", "t0k")
    a, b = _registered(fresh), _registered(fresh)
    _pf_and_report(monkeypatch, b["api_key"])
    client.post("/admin/evaluation/participants", json={
        "agent_id": a["id"], "basis": "operator_outside_project_team",
        "public_source": "https://example.org/a"}, headers={"X-Admin-Token": "t0k"})
    s = client.get("/preflight/outcomes").json()
    assert s["independently_demonstrated_benefit"] is False
    assert s["independent"]["attested_participants"] == 1
    assert s["independent"]["decisions"] == 0


# ==========================================================================
# F5 — MCP carries the same participant attribution as HTTP, key never stored
# ==========================================================================
def _data(res):
    d = res.data if hasattr(res, "data") else res
    if isinstance(d, list):
        d = json.loads(d[0].text)
    return d


def _mcp_fake_run(monkeypatch):
    def fake_run(url, *, store=None):
        return {"target": url, "verdict": "do_not_delegate", "headline": "x",
                "checks": [], "failed": ["protocol_handshake"],
                "unknowns": ["independent_evidence"],
                "scored": [c for c in ALL if c != "independent_evidence"],
                "method": "m", "limits": "l"}
    monkeypatch.setattr(preflight, "run", fake_run)


def test_f5_mcp_api_key_argument_joins_both_legs_as_registered_participant(
        fresh, monkeypatch):
    _mcp_fake_run(monkeypatch)
    agent = _registered(fresh)

    async def go():
        async with Client(guild_mcp) as c:
            pf = _data(await c.call_tool("guild_preflight", {
                "url": "https://x.example/a", "api_key": agent["api_key"]}))
            assert pf["verdict"] == "do_not_delegate"
            rd = _data(await c.call_tool("guild_preflight_outcome", {
                "preflight_id": pf["preflight_id"], "action": "declined",
                "baseline_direct_check": "failure", "detail": "protocol_error",
                "decision_basis": "ag_evidence", "api_key": agent["api_key"]}))
            assert rd["recorded"] is True
            assert rd["provenance"] == "registered_participant"
    asyncio.run(go())
    evs = [e for e in fresh.events if e["type"] in ("preflight_run", "preflight_outcome")]
    assert len(evs) == 2
    for e in evs:
        assert e["participant_agent_id"] == agent["id"]
        assert e["transport"] == "mcp"
        assert agent["api_key"] not in json.dumps(e)       # secret never recorded
    s = client.get("/preflight/outcomes").json()
    assert s["by_ownership"]["registered_participant"]["decisions"] == 1
    assert s["by_ownership"]["registered_participant"]["benefit"] == 1
    assert s["independently_demonstrated_benefit"] is False   # still needs attestation


def test_f5_mcp_key_via_http_header_is_honoured(fresh, monkeypatch):
    _mcp_fake_run(monkeypatch)
    agent = _registered(fresh)
    monkeypatch.setattr(mcp_module, "_http_headers_for_attribution",
                        lambda: {"x-api-key": agent["api_key"]})

    async def go():
        async with Client(guild_mcp) as c:
            pf = _data(await c.call_tool("guild_preflight", {"url": "https://x.example/a"}))
            assert pf["preflight_id"].startswith("pf_")
    asyncio.run(go())
    run = next(e for e in fresh.events if e["type"] == "preflight_run")
    assert run["participant_agent_id"] == agent["id"]


def test_f5_mcp_invalid_key_is_refused_not_downgraded(fresh, monkeypatch):
    _mcp_fake_run(monkeypatch)

    async def go():
        async with Client(guild_mcp) as c:
            pf = _data(await c.call_tool("guild_preflight", {
                "url": "https://x.example/a", "api_key": "nope"}))
            assert pf["kind"] == "preflight_participant_invalid"
            assert "preflight_id" not in pf
            rd = _data(await c.call_tool("guild_preflight_outcome", {
                "preflight_id": "pf_" + "a" * 20, "action": "called",
                "observed": "success", "api_key": "nope"}))
            assert rd["kind"] == "preflight_participant_invalid"
    asyncio.run(go())
    assert not [e for e in fresh.events if e["type"] in ("preflight_run", "preflight_outcome")]


def test_f5_mcp_without_a_key_is_still_anonymous(fresh, monkeypatch):
    _mcp_fake_run(monkeypatch)

    async def go():
        async with Client(guild_mcp) as c:
            pf = _data(await c.call_tool("guild_preflight", {"url": "https://x.example/a"}))
            assert pf["preflight_id"].startswith("pf_")
    asyncio.run(go())
    run = next(e for e in fresh.events if e["type"] == "preflight_run")
    assert not run.get("participant_agent_id")


# ==========================================================================
# F6 — malformed targets and non-object cards are handled, never raised
# ==========================================================================
BAD_TARGETS = ("http://example.com:99999/a2a", "http://example.com:abc/",
               "http://[::1/", "http://[zz]/", "ftp://example.com/", "", "x")


@pytest.mark.parametrize("target", BAD_TARGETS)
def test_f6_run_never_raises_and_says_why(target):
    out = preflight.run(target)
    assert out["verdict"] == "do_not_delegate"
    assert out["input_error"]
    assert out["method"].startswith("input validation only")


@pytest.mark.parametrize("target", BAD_TARGETS[:4])
def test_f6_http_route_answers_422_and_stamps_no_run(fresh, target):
    r = client.get("/preflight", params={"url": target}, headers={"User-Agent": UA})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_target_url"
    assert not [e for e in fresh.events if e["type"] == "preflight_run"]


def test_f6_mcp_tool_returns_an_error_object_for_a_bad_target(fresh):
    async def go():
        async with Client(guild_mcp) as c:
            out = _data(await c.call_tool("guild_preflight", {"url": "http://[::1/"}))
            assert out["kind"] == "preflight_target_invalid"
    asyncio.run(go())


@pytest.mark.parametrize("body", [b"[1,2]", b'"str"', b"42", b"null", b"true"])
def test_f6_non_object_json_card_is_not_a_card(monkeypatch, body):
    _fake_net(monkeypatch, card=body)
    out = preflight.run(TARGET)
    res = _check(out, "agent_card_resolves")
    assert res["status"] == "failed" and "not an object" in res["detail"]
    assert _check(out, "agent_card_signed")["status"] == "unknown"
    assert out["verdict"] == "delegate_with_caution"


def test_f6_url_policy_check_reports_malformed_urls_as_data():
    for t in BAD_TARGETS[:4]:
        ok, reason = reachability.url_policy_check(t)
        assert ok is False and reason


def test_f6_a_probe_exception_is_contained_and_claims_nothing(monkeypatch):
    """Review findings 2–3: an internal error must not report a failed
    reachability check (it observed nothing) and must not be creditable."""
    def boom(url, **kw):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(reachability, "liveness_probe", boom)
    out = preflight.run(TARGET)
    assert out["verdict"] == "delegate_with_caution"
    assert "RuntimeError" in out["input_error"]
    assert out["failed"] == [] and out["scored"] == []
    assert all(c["status"] == "unknown" for c in out["checks"])
    assert "no network request" not in out["method"]
    run = {"verdict": out["verdict"], "failed_checks": out["failed"],
           "unknown_checks": out["unknowns"], "scored_checks": out["scored"]}
    c = po.classify(run, _rec("unreachable"))
    assert c["kind"] != "benefit" and c["result"] == "unknown_check_not_covered"
    c = po.classify(run, po.validate({"preflight_id": "pf_" + "a" * 20,
                                      "action": "called", "observed": "failure",
                                      "detail": "unreachable", "reporter_kind": "agent"}))
    assert c["kind"] != "mistake"
