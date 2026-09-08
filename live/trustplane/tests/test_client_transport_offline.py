"""Offline regression tests for GuildClient transport semantics.

Every test runs against a loopback ``http.server`` fake or a captured live
fixture (``tests/fixtures/``, unpaid first-party captures from 2026-09-08).
No network beyond 127.0.0.1, no payment, no environment reads. Each case
names the defect it guards against, reproduced on the pristine client
(b341a475) before the fix:

* a 402 quote was swallowed as an "outage";
* urllib followed redirects and replayed ``X-API-Key`` to the new host;
* response bodies were read without a bound;
* an expired passport, or one about a different subject, was returned.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentguild_trustplane import verify as V
from agentguild_trustplane.cache import SignedDecisionCache
from agentguild_trustplane.client import (
    MAX_RESPONSE_BYTES, GuildClient, GuildHTTPError, GuildRedirect,
    PaymentRequired, ResponseTooLarge, decode_payment_required_header,
    quote_terms,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeGuild:
    """Loopback HTTP server; ``routes`` maps a path prefix to a callable
    returning (status, headers, body_bytes). Records every request."""

    def __init__(self) -> None:
        self.routes: dict = {}
        self.requests: list = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _serve(self, body_in: bytes = b"") -> None:
                srv.requests.append((self.command, self.path,
                                     {k.lower(): v for k, v in self.headers.items()},
                                     body_in))
                for prefix, fn in srv.routes.items():
                    if self.path.startswith(prefix):
                        status, headers, body = fn(self)
                        break
                else:
                    status, headers, body = 404, {"Content-Type": "application/json"}, b'{"detail":"not found"}'
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._serve()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._serve(self.rfile.read(n))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def json(self, prefix: str, status: int, obj, headers: dict | None = None) -> None:
        body = json.dumps(obj).encode()
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        self.routes[prefix] = lambda h: (status, hdrs, body)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def guild():
    g = FakeGuild()
    yield g
    g.close()


# --- a test issuer that signs like the Guild (eddsa-jcs-2022 / did:key) ------
class Issuer:
    def __init__(self) -> None:
        self.sk = Ed25519PrivateKey.generate()
        pub = self.sk.public_key().public_bytes_raw()
        self.did = "did:key:z" + V.b58encode(b"\xed\x01" + pub)

    def sign(self, doc: dict) -> dict:
        proof = {"type": "DataIntegrityProof", "cryptosuite": "eddsa-jcs-2022",
                 "created": "2026-09-08T00:00:00Z",
                 "verificationMethod": f"{self.did}#{self.did[8:]}",
                 "proofPurpose": "assertionMethod"}
        sig = self.sk.sign(V._hash_data(doc, proof))
        return {**doc, "proof": {**proof, "proofValue": "z" + V.b58encode(sig)}}

    def passport(self, subject_did: str, local_id: str = "agent_1",
                 days_ago: int = 0, ttl_days: int = 7, **claims) -> dict:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days_ago)
        return self.sign({
            "@context": ["https://www.w3.org/ns/credentials/v2"],
            "id": f"urn:passport:{local_id}:{int(start.timestamp())}",
            "type": ["VerifiableCredential", "AgentGuildPassport"],
            "issuer": self.did,
            "validFrom": start.isoformat(),
            "validUntil": (start + timedelta(days=ttl_days)).isoformat(),
            "credentialSubject": {"id": subject_did, "trust": 40.0, **claims},
        })


# --- 402: surfaced, never paid ------------------------------------------------
def test_402_on_signed_decision_is_payment_required_not_outage(guild):
    fx = fixture("live_check_402_2026-09-08.json")
    guild.json("/check", 402, fx["body"],
               {"PAYMENT-REQUIRED": fx["headers"]["payment-required"]})
    c = GuildClient(guild.base)
    env, channel, age = c.signed_decision("code-review")
    assert env is None and age is None
    assert channel == "payment_required"           # was "outage" before
    assert c.stats["payment_required"] == 1
    pr = c.last_payment_required
    assert isinstance(pr, PaymentRequired) and pr.status == 402
    # the terms come from the response, not a price table
    terms = pr.terms
    assert terms and terms[0]["scheme"] == "exact"
    assert terms[0]["network"] == fx["body"]["accepts"][0]["network"]
    assert terms[0]["amount"] == fx["body"]["accepts"][0]["amount"]
    assert terms[0]["payTo"] == fx["body"]["accepts"][0]["payTo"]
    assert terms[0]["resource"].startswith("https://agent-guild-5d5r.onrender.com/check?")
    assert "not paid" in str(pr)
    # exactly one request was made: no retry, no payment header, no key
    assert len(guild.requests) == 1
    hdrs = guild.requests[0][2]
    assert "payment-signature" not in hdrs and "x-api-key" not in hdrs


def test_quote_reports_payment_required_without_paying(guild):
    fx = fixture("live_check_402_2026-09-08.json")
    guild.json("/check", 402, fx["body"],
               {"PAYMENT-REQUIRED": fx["headers"]["payment-required"]})
    q = GuildClient(guild.base).quote("code-review")
    assert q["status"] == "payment_required"
    assert q["quote"]["x402Version"] == 2
    assert q["terms"][0]["amount"] == "1000000"
    assert q["payment_required_header"] == fx["headers"]["payment-required"]
    assert len(guild.requests) == 1


def test_quote_reports_served_document_when_free(guild):
    guild.json("/check", 200, {"decision": {"contract": "AGD-1/1.0"}})
    q = GuildClient(guild.base).quote("code-review", signed=False)
    assert q["status"] == "served" and "decision" in q["document"]


def test_payment_required_header_is_authoritative_for_terms():
    fx = fixture("live_check_402_2026-09-08.json")
    hdr = fx["headers"]["payment-required"]
    decoded = decode_payment_required_header(hdr)
    assert decoded and decoded["x402Version"] == 2
    # header wins over a body that disagrees
    terms = quote_terms({"accepts": [{"amount": "1", "scheme": "bogus"}]}, hdr)
    assert terms[0]["amount"] == "1000000" and terms[0]["scheme"] == "exact"
    assert quote_terms(None, None) == []
    assert decode_payment_required_header("not base64!!") is None


# --- redirects: refused, credentials never replayed -----------------------------
def test_redirect_is_refused_and_api_key_not_forwarded(guild):
    other = FakeGuild()
    try:
        other.json("/check", 200, {"hello": 1})
        guild.routes["/check"] = lambda h: (302, {"Location": other.base + "/check?x=1"}, b"")
        c = GuildClient(guild.base, api_key="SECRET-KEY")
        with pytest.raises(GuildRedirect) as ei:
            c._get("/check?capability=x")
        assert ei.value.status == 302 and ei.value.location.startswith(other.base)
        assert other.requests == []                 # nothing reached the other origin
        assert c.stats["redirects_refused"] == 1
        # and the gateway-facing call treats it as no evidence
        env, channel, _ = c.signed_decision("x")
        assert env is None and channel == "outage"
        assert other.requests == []
    finally:
        other.close()


def test_post_redirect_is_refused_too(guild):
    guild.routes["/outcomes"] = lambda h: (307, {"Location": "http://127.0.0.1:9/outcomes"}, b"")
    c = GuildClient(guild.base, api_key="SECRET-KEY")
    with pytest.raises(GuildRedirect):
        c._post("/outcomes", {"a": 1})
    assert c.post_signed_outcome({"a": 1}) is None    # existing caller contract


# --- response bound ---------------------------------------------------------------
def test_oversized_body_is_rejected(guild):
    big = b'{"pad":"' + b"x" * (MAX_RESPONSE_BYTES + 16) + b'"}'
    guild.routes["/check"] = lambda h: (200, {"Content-Type": "application/json"}, big)
    c = GuildClient(guild.base)
    with pytest.raises(ResponseTooLarge):
        c._get("/check?capability=x")
    env, channel, _ = c.signed_decision("x")
    assert env is None and channel == "outage"


def test_body_at_the_bound_is_accepted(guild):
    body = json.dumps({"pad": "x" * (MAX_RESPONSE_BYTES - 32)}).encode()
    assert len(body) <= MAX_RESPONSE_BYTES
    guild.routes["/check"] = lambda h: (200, {"Content-Type": "application/json"}, body)
    assert len(GuildClient(guild.base)._get("/check?capability=x")["pad"]) == MAX_RESPONSE_BYTES - 32


def test_non_json_and_http_errors_are_typed(guild):
    guild.routes["/check"] = lambda h: (200, {"Content-Type": "text/html"}, b"<html>")
    with pytest.raises(GuildHTTPError):
        GuildClient(guild.base)._get("/check?capability=x")
    guild.json("/check", 503, {"detail": "down"})
    c = GuildClient(guild.base)
    with pytest.raises(GuildHTTPError) as ei:
        c._get("/check?capability=x")
    assert ei.value.status == 503 and ei.value.body == {"detail": "down"}
    assert c.signed_decision("x")[1] == "outage"


# --- passports: verified, fresh, bound to the requested subject ------------------
def test_live_passport_fixture_verifies_and_binds():
    fx = fixture("live_passport_2026-09-08.json")
    doc = fx["body"]
    v = V.verify_data_integrity(doc)
    assert v["verified"] and v["issuer_did"] == fixture("live_issuer_2026-09-08.json")["body"]["did"]
    from agentguild_trustplane.contract import passport_binding_violation as _passport_subject_ok
    assert _passport_subject_ok(doc["credentialSubject"]["id"], doc) is None
    assert _passport_subject_ok("agent_d0a8f6ef9b41", doc) is None          # local id via urn
    assert "subject mismatch" in _passport_subject_ok("did:key:zSomebodyElse", doc)
    assert "subject mismatch" in _passport_subject_ok("agent_other", doc)
    tampered = {**doc, "credentialSubject": {**doc["credentialSubject"], "trust": 99.9}}
    assert not V.verify_data_integrity(tampered)["verified"]


def test_passport_accepted_only_when_verified_fresh_and_about_the_subject(guild):
    iss = Issuer()
    good = iss.passport("did:key:zSubject", "agent_1")
    guild.json("/agents/", 200, good)
    c = GuildClient(guild.base)
    r = c.passport_result("did:key:zSubject")
    assert r.ok and r.channel == "live" and r.subject_did == "did:key:zSubject"
    assert r.issuer_did == iss.did and r.age_seconds is not None
    assert c.passport("agent_1") == good                      # local id binds via urn

    # wrong subject -> unverified, no doc (was returned before the fix)
    r = c.passport_result("did:key:zOther")
    assert not r.ok and r.channel == "unverified" and "subject mismatch" in r.reason
    assert c.passport("did:key:zOther") is None
    assert c.passport("agent_2") is None

    # expired -> unverified (was returned before the fix)
    guild.json("/agents/", 200, iss.passport("did:key:zSubject", days_ago=30))
    r = GuildClient(guild.base).passport_result("did:key:zSubject")
    assert not r.ok and r.reason == "outside validity window"

    # not a passport credential type
    bad_type = iss.sign({**{k: v for k, v in good.items() if k != "proof"},
                         "type": ["VerifiableCredential"]})
    guild.json("/agents/", 200, bad_type)
    assert "not an AgentGuildPassport" in GuildClient(guild.base).passport_result("did:key:zSubject").reason

    # unsigned / tampered
    guild.json("/agents/", 200, {**good, "credentialSubject": {"id": "did:key:zSubject", "trust": 99}})
    r = GuildClient(guild.base).passport_result("did:key:zSubject")
    assert not r.ok and r.reason.startswith("proof:")


def test_passport_404_and_402_are_reported_not_silent(guild):
    c = GuildClient(guild.base)
    r = c.passport_result("agent_none")
    assert not r.ok and r.channel == "not_found"
    assert c.passport("agent_none") is None
    fx = fixture("live_check_402_2026-09-08.json")
    guild.json("/agents/", 402, fx["body"], {"PAYMENT-REQUIRED": fx["headers"]["payment-required"]})
    r = c.passport_result("agent_x")
    assert r.channel == "payment_required" and "not paid" in r.reason
    assert c.last_payment_required is not None and c.stats["payment_required"] == 1


def test_passport_cache_fallback_is_reverified_and_rebound(guild, tmp_path):
    iss = Issuer()
    cache = SignedDecisionCache(tmp_path / "cache")
    good = iss.passport("did:key:zSubject", "agent_1")
    guild.json("/agents/", 200, good)
    live = GuildClient(guild.base, cache=cache)
    assert live.passport_result("did:key:zSubject").channel == "live"
    dead = GuildClient("http://127.0.0.1:9", cache=cache, timeout=0.5)
    r = dead.passport_result("did:key:zSubject")
    assert r.ok and r.channel == "cache" and r.state == "fresh"
    # a cached credential is never served for a different subject
    r = dead.passport_result("did:key:zOther")
    assert not r.ok and r.channel == "outage"
    assert dead.passport("did:key:zSubject") == good


# --- free preflight ------------------------------------------------------------------
def test_preflight_parses_live_fixture_and_keeps_unknowns_explicit(guild):
    fx = fixture("live_preflight_2026-09-08.json")
    guild.json("/preflight", 200, fx["body"])
    r = GuildClient(guild.base).preflight("https://agent-guild-5d5r.onrender.com/mcp")
    assert r.verdict == "delegate_with_caution" and r.known_verdict
    assert not r.no_failed_checks and not r.do_not_delegate
    assert r.failed == ["agent_card_signed"]
    assert set(r.unknowns) == {"payment_claim_holds", "independent_evidence"}
    assert not set(r.unknowns) & set(r.scored)          # unknowns never scored
    assert r.raw == fx["body"] and "NOT a cached badge" in r.method
    req = guild.requests[0]
    assert req[0] == "GET" and req[1].startswith("/preflight?url=https%3A%2F%2F")
    assert "x-api-key" not in req[2]                     # free: no credentials sent


def test_preflight_unknown_verdict_is_flagged_not_normalised(guild):
    guild.json("/preflight", 200, {"target": "x", "verdict": "totally_fine"})
    r = GuildClient(guild.base).preflight("https://example.com")
    assert r.verdict == "totally_fine" and not r.known_verdict


def test_preflight_errors_propagate(guild):
    guild.json("/preflight", 200, {"no": "verdict"})
    with pytest.raises(GuildHTTPError):
        GuildClient(guild.base).preflight("https://example.com")
    with pytest.raises(Exception):
        GuildClient("http://127.0.0.1:9", timeout=0.5).preflight("https://example.com")


# --- explicit registration ------------------------------------------------------
def test_register_is_explicit_and_never_adopts_or_logs_the_key(guild, capsys):
    resp = {"id": "agent_abc", "did": "did:key:zNew", "public_key": "aa",
            "capabilities": ["fact-check"], "custodial": True,
            "api_key": "ONE-TIME-SECRET", "guild_next": {}, "listing": {}}
    guild.json("/agents/register", 200, resp)
    c = GuildClient(guild.base)
    out = c.register("my-agent", ["fact-check"], metadata={"endpoint": "https://a.example"},
                     principal="org:example")
    assert out == resp
    assert c.api_key is None                              # not adopted
    assert "ONE-TIME-SECRET" not in capsys.readouterr().out
    method, path, hdrs, body = guild.requests[0]
    assert method == "POST" and path == "/agents/register"
    sent = json.loads(body)
    assert sent == {"name": "my-agent", "capabilities": ["fact-check"],
                    "metadata": {"endpoint": "https://a.example"},
                    "principal": "org:example", "src": "pypi:agentguild-trustplane"}
    assert "seed" not in sent
    for h in ("x-admin-token", "x-agent-guild-first-party", "x-guild-source", "x-api-key"):
        assert h not in hdrs
    # src can be omitted; malformed src / inputs are rejected before any request
    c.register("n", [], src=None)
    assert "src" not in json.loads(guild.requests[1][3])
    with pytest.raises(ValueError):
        c.register("", ["x"])
    with pytest.raises(ValueError):
        c.register("n", ["x"], src="Bad Src!")
    assert len(guild.requests) == 2


def test_nothing_registers_implicitly(guild):
    guild.json("/preflight", 200, {"target": "x", "verdict": "no_failed_checks"})
    guild.json("/check", 200, {})
    c = GuildClient(guild.base)
    c.preflight("https://example.com")
    c.quote("x")
    c.signed_decision("x")
    c.passport("agent_x")
    assert not any(p.startswith("/agents/register") for _, p, _, _ in guild.requests)
    assert all(m == "GET" for m, _, _, _ in guild.requests)


# --- extra headers (operator first-party marking) travel, but are never inspected --
def test_extra_headers_are_sent_verbatim(guild):
    guild.json("/preflight", 200, {"target": "x", "verdict": "no_failed_checks"})
    GuildClient(guild.base, extra_headers={"X-Agent-Guild-First-Party": "tok",
                                           "X-Agent-Guild-Role": "test"}
                ).preflight("https://example.com")
    hdrs = guild.requests[0][2]
    assert hdrs["x-agent-guild-first-party"] == "tok" and hdrs["x-agent-guild-role"] == "test"
    assert hdrs["user-agent"].startswith("agentguild-trustplane/")
