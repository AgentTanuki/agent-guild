"""SSRF-safe declaration-time reachability verifier (refined 2026-07-10).

Owner-initiated, runs ONLY at declaration, never from a read path. Rejects
prohibited endpoint properties; preserves a policy-valid-but-down declaration.
INVOCATION_VERIFIED comes ONLY from a trusted AG-originated invocation bound to
a unique id against the CURRENT endpoint — never from a submitted receipt.
"""
import os, socket, ssl, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import pytest

os.environ.setdefault("GUILD_DATA", os.path.join(tempfile.mkdtemp(), "g.json"))

import app.reachability as R
from app.store import Store


def _fresh():
    import uuid
    return Store(path=os.path.join(tempfile.mkdtemp(), f"{uuid.uuid4().hex}.json"))


def _ai(addr, family=socket.AF_INET):
    return [(family, 1, 6, "", (addr, 443))]


# --- URL policy --------------------------------------------------------------
@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a2a", True), ("http://example.com:8080/x", True),
    ("https://user:pass@example.com", False), ("ftp://example.com", False),
    ("https://example.com:22", False), ("http://127.0.0.1/x", False),
    ("http://10.0.0.5/x", False), ("http://169.254.1.1/x", False),
    ("http://[::1]/x", False), ("http://0.0.0.0/x", False),
    ("http://224.0.0.1/x", False), ("not-a-url", False),
])
def test_url_policy(url, ok):
    assert R.url_policy_check(url)[0] is ok


def test_declaration_rejects_prohibited_accepts_public():
    s = _fresh(); a = s.register_agent("R", ["x"], {})
    with pytest.raises(ValueError):
        s.set_agent_endpoint(a["id"], "http://127.0.0.1:9000")
    out = s.set_agent_endpoint(a["id"], "https://example.com/a2a")
    assert out["reachability_status"] == "declared_unverified"
    assert out["recommended_for_routing"] is False
    assert out["endpoint_fingerprint"] and out["evidence_level"] == "none"


# --- rebinding: DNS -> private (v4 and v6) refused ---------------------------
@pytest.mark.parametrize("addr,fam", [("10.1.2.3", socket.AF_INET),
                                      ("fd00::1", socket.AF_INET6)])
def test_dns_to_private_refused_not_rejected(addr, fam):
    s = _fresh(); a = s.register_agent("Rb", ["x"], {})
    with mock.patch.object(R.socket, "getaddrinfo", return_value=_ai(addr, fam)):
        out = s.set_agent_endpoint(a["id"], "https://rebind.example/a2a", verify=True)
    assert out["reachability_status"] == "currently_unreachable"
    assert s.get_agent(a["id"])["metadata"]["endpoint"] == "https://rebind.example/a2a"


# --- liveness outcomes: weak HTTP is NOT routable ----------------------------
def test_generic_405_is_http_responsive_not_routable():
    s = _fresh(); a = s.register_agent("H", ["x"], {})
    calls = {"n": 0}
    def fake(*args, **kw):
        calls["n"] += 1
        return (405, b"") if calls["n"] == 1 else (200, b"ok")  # HEAD 405, GET 200
    with mock.patch.object(R.socket, "getaddrinfo", return_value=_ai("93.184.216.34")), \
         mock.patch.object(R, "_http_request_pinned", side_effect=fake):
        out = s.set_agent_endpoint(a["id"], "https://api.example/", verify=True)
    assert out["reachability_status"] == "http_responsive"
    assert out["evidence_level"] == "http_response"
    assert out["recommended_for_routing"] is False       # weak != protocol


def test_401_is_http_responsive_not_routable():
    s = _fresh(); a = s.register_agent("Auth", ["x"], {})
    with mock.patch.object(R.socket, "getaddrinfo", return_value=_ai("93.184.216.34")), \
         mock.patch.object(R, "_http_request_pinned", return_value=(401, b"")):
        out = s.set_agent_endpoint(a["id"], "https://api.example/", verify=True)
    assert out["reachability_status"] == "http_responsive"
    assert out["recommended_for_routing"] is False


def test_a2a_card_handshake_is_recently_reachable_and_routable():
    s = _fresh(); a = s.register_agent("A2A", ["x"], {})
    card = b'{"protocolVersion":"0.3.0","skills":[{"id":"x"}]}'
    with mock.patch.object(R.socket, "getaddrinfo", return_value=_ai("93.184.216.34")), \
         mock.patch.object(R, "_http_request_pinned", return_value=(200, card)):
        out = s.set_agent_endpoint(a["id"], "https://prov.example/a2a", verify=True)
    assert out["reachability_status"] == "recently_reachable"
    assert out["evidence_level"] == "protocol_handshake"
    assert out["recommended_for_routing"] is True


def test_mcp_initialise_handshake_is_recently_reachable():
    s = _fresh(); a = s.register_agent("MCP", ["x"], {})
    body = b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26"}}'
    with mock.patch.object(R.socket, "getaddrinfo", return_value=_ai("93.184.216.34")), \
         mock.patch.object(R, "_http_request_pinned", return_value=(200, body)):
        out = s.set_agent_endpoint(a["id"], "https://prov.example/mcp/", verify=True)
    assert out["reachability_status"] == "recently_reachable"
    assert out["evidence_level"] == "protocol_handshake"


def test_redirect_and_timeout_are_currently_unreachable():
    s = _fresh(); a = s.register_agent("RT", ["x"], {})
    with mock.patch.object(R.socket, "getaddrinfo", return_value=_ai("93.184.216.34")), \
         mock.patch.object(R, "_http_request_pinned", return_value=(302, b"")):
        out = s.set_agent_endpoint(a["id"], "https://x.example/", verify=True)
    assert out["reachability_status"] == "currently_unreachable"
    with mock.patch.object(R.socket, "getaddrinfo", return_value=_ai("93.184.216.34")), \
         mock.patch.object(R, "_http_request_pinned", side_effect=socket.timeout()):
        out = s.set_agent_endpoint(a["id"], "https://x.example/", verify=True)
    assert out["reachability_status"] == "currently_unreachable"
    assert s.get_agent(a["id"])["metadata"]["endpoint"]     # declaration intact


# --- endpoint change invalidates prior evidence ------------------------------
def test_endpoint_change_invalidates_evidence():
    s = _fresh(); a = s.register_agent("Chg", ["x"], {})
    card = b'{"skills":[]}'
    with mock.patch.object(R.socket, "getaddrinfo", return_value=_ai("93.184.216.34")), \
         mock.patch.object(R, "_http_request_pinned", return_value=(200, card)):
        s.set_agent_endpoint(a["id"], "https://old.example/a2a", verify=True)
    assert s.get_agent(a["id"])["reachability"]["status"] == "recently_reachable"
    # declaring a NEW endpoint drops the old record; status falls back
    out = s.set_agent_endpoint(a["id"], "https://new.example/a2a")
    assert out["reachability_status"] == "declared_unverified"
    assert "reachability" not in s.get_agent(a["id"])


def test_expired_evidence_not_recommended_for_routing():
    s = _fresh(); a = s.register_agent("Exp", ["x"], {})
    card = b'{"skills":[]}'
    with mock.patch.object(R.socket, "getaddrinfo", return_value=_ai("93.184.216.34")), \
         mock.patch.object(R, "_http_request_pinned", return_value=(200, card)):
        s.set_agent_endpoint(a["id"], "https://prov.example/a2a", verify=True)
    rec = s.get_agent(a["id"])["reachability"]
    rec["expires_at"] = "2000-01-01T00:00:00+00:00"          # force expiry
    ep = s.get_agent(a["id"])["metadata"]["endpoint"]
    f = R.reachability_fields(ep, rec)
    assert f["reachability_status"] == "declared_unverified"
    assert f["recommended_for_routing"] is False


# --- INVOCATION_VERIFIED only from a trusted AG invocation -------------------
def test_receipt_cannot_self_upgrade_reachability():
    s = _fresh()
    emp = s.register_agent("Employer", ["hire"], {})
    w = s.register_agent("Worker", ["x"], {})
    s.set_agent_endpoint(w["id"], "https://w.example/a2a")
    t = s.create_task(emp["id"], w["id"], "x", 0.0, {})
    s.submit_receipt(t["id"], "0xabc", outcome="delivered")   # worker self-reports
    rec = s.get_agent(w["id"]).get("reachability")
    assert rec is None                                        # NOT upgraded


def test_receipt_referencing_unknown_invocation_does_not_verify():
    s = _fresh()
    assert s.complete_outbound_invocation("oinv_doesnotexist", protocol_ok=True) is False


def test_only_ag_originated_invocation_against_current_endpoint_verifies():
    s = _fresh(); w = s.register_agent("W", ["x"], {})
    s.set_agent_endpoint(w["id"], "https://w.example/a2a")
    inv = s.begin_outbound_invocation(w["id"])
    assert inv and inv["invocation_id"].startswith("oinv_")
    assert s.complete_outbound_invocation(inv["invocation_id"], protocol_ok=True) is True
    rec = s.get_agent(w["id"])["reachability"]
    assert rec["status"] == "invocation_verified"
    assert rec["evidence_level"] == "guild_invocation"
    assert rec["invocation_id"] == inv["invocation_id"]


def test_stale_invocation_against_previous_endpoint_does_not_verify():
    s = _fresh(); w = s.register_agent("W", ["x"], {})
    s.set_agent_endpoint(w["id"], "https://old.example/a2a")
    inv = s.begin_outbound_invocation(w["id"])
    s.set_agent_endpoint(w["id"], "https://new.example/a2a")   # endpoint changed
    assert s.complete_outbound_invocation(inv["invocation_id"], protocol_ok=True) is False
    assert s.get_agent(w["id"]).get("reachability", {}).get("status") != "invocation_verified"


def test_protocol_failure_does_not_verify():
    s = _fresh(); w = s.register_agent("W", ["x"], {})
    s.set_agent_endpoint(w["id"], "https://w.example/a2a")
    inv = s.begin_outbound_invocation(w["id"])
    assert s.complete_outbound_invocation(inv["invocation_id"], protocol_ok=False) is False


# --- read paths remain network-free ------------------------------------------
def test_read_paths_never_probe():
    s = _fresh(); a = s.register_agent("RO", ["fact-check"], {})
    s.set_agent_endpoint(a["id"], "https://example.com/a2a")
    def boom(*a, **k):
        raise AssertionError("read path performed DNS/network access")
    with mock.patch.object(R.socket, "getaddrinfo", side_effect=boom):
        s.shortlist("fact-check", limit=5)
        s.check("fact-check")
        R.reachability_fields("https://example.com/a2a", a.get("reachability"))


# --- concurrency: dedup identical in-flight verifications --------------------
def test_duplicate_inflight_verification_is_deduped():
    s = _fresh(); a = s.register_agent("Dup", ["x"], {})
    R._inflight.add(f"{a['id']}|https://busy.example/a2a")   # pretend one in flight
    try:
        out = s.set_agent_endpoint(a["id"], "https://busy.example/a2a", verify=True)
        assert out["reachability_status"] == "verification_inconclusive"
    finally:
        R._inflight.discard(f"{a['id']}|https://busy.example/a2a")


# --- HTTPS pinning: SNI + cert-hostname validation (integration) -------------
def _self_signed(cn="localhost"):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime as dt
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime.utcnow() - dt.timedelta(days=1))
            .not_valid_after(dt.datetime.utcnow() + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), False)
            .sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    cp, kp = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    open(cp, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(kp, "wb").write(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return cp, kp


class _H(BaseHTTPRequestHandler):
    def do_HEAD(self):
        _H.sni = getattr(self.connection, "server_hostname", None)
        self.send_response(200); self.end_headers()
    def log_message(self, *a):
        pass


def _https_server(certfile, keyfile):
    srv = HTTPServer(("127.0.0.1", 0), _H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    return srv, srv.server_address[1]


def test_tls_pinning_sni_and_hostname_validation():
    cp, kp = _self_signed("localhost")
    srv, port = _https_server(cp, kp)
    try:
        trust = ssl.create_default_context(); trust.load_verify_locations(cp)
        # connect to the PINNED loopback IP but validate cert for hostname
        code, _ = R._http_request_pinned("https", "localhost", socket.AF_INET,
                                         "127.0.0.1", port, "/", method="HEAD",
                                         ssl_context=trust)
        assert code == 200
        # a wrong hostname must fail cert-hostname validation (not the Host header)
        with pytest.raises(ssl.SSLError):
            R._http_request_pinned("https", "wrong.example", socket.AF_INET,
                                   "127.0.0.1", port, "/", method="HEAD",
                                   ssl_context=trust)
        # an UNTRUSTED cert (default system context) must fail verification
        with pytest.raises(ssl.SSLError):
            R._http_request_pinned("https", "localhost", socket.AF_INET,
                                   "127.0.0.1", port, "/", method="HEAD")
    finally:
        srv.shutdown()


# --- keepalive event dedup (2026-07-14) ---------------------------------------
# A worker that re-declares the SAME endpoint on a timer (verify=True) must not
# flood the journal: within KEEPALIVE_EVENT_WINDOW_S an unchanged endpoint with
# an unchanged probe outcome records NO new events. Endpoint changes, status
# transitions, and verify=False declarations always record.

def _ev(s, etype):
    return [e for e in s.events if e.get("type") == etype]


def _probe_ok():
    card = b'{"protocolVersion":"0.3.0","skills":[{"id":"x"}]}'
    return (mock.patch.object(R.socket, "getaddrinfo",
                              return_value=_ai("93.184.216.34")),
            mock.patch.object(R, "_http_request_pinned", return_value=(200, card)))


def test_keepalive_repeat_declare_same_outcome_records_no_new_events():
    s = _fresh(); a = s.register_agent("KA", ["x"], {})
    g, h = _probe_ok()
    with g, h:
        s.set_agent_endpoint(a["id"], "https://w.example/a2a", verify=True)
        base_d = len(_ev(s, "endpoint_declared"))
        base_v = len(_ev(s, "endpoint_verification"))
        assert base_d == 1 and base_v == 1
        for _ in range(5):   # the 2-minute keepalive pattern
            out = s.set_agent_endpoint(a["id"], "https://w.example/a2a", verify=True)
    # freshness still served on every call…
    assert out["reachability_status"] == "recently_reachable"
    assert s.get_agent(a["id"])["reachability"]["status"] == "recently_reachable"
    # …but zero additional journal events
    assert len(_ev(s, "endpoint_declared")) == base_d
    assert len(_ev(s, "endpoint_verification")) == base_v


def test_keepalive_status_transition_is_never_hidden():
    s = _fresh(); a = s.register_agent("KT", ["x"], {})
    g, h = _probe_ok()
    with g, h:
        s.set_agent_endpoint(a["id"], "https://w.example/a2a", verify=True)
    # worker goes DOWN inside the window: verification event MUST record
    with mock.patch.object(R.socket, "getaddrinfo", return_value=_ai("93.184.216.34")), \
         mock.patch.object(R, "_http_request_pinned", side_effect=socket.timeout()):
        s.set_agent_endpoint(a["id"], "https://w.example/a2a", verify=True)
    vs = _ev(s, "endpoint_verification")
    assert [v["reachability_status"] for v in vs] == \
        ["recently_reachable", "currently_unreachable"]
    # recovery inside the window records again (transition, not a repeat)
    with g, h:
        s.set_agent_endpoint(a["id"], "https://w.example/a2a", verify=True)
    vs = _ev(s, "endpoint_verification")
    assert vs[-1]["reachability_status"] == "recently_reachable"
    assert len(vs) == 3


def test_endpoint_change_and_expired_window_always_record():
    import app.store as store_mod
    s = _fresh(); a = s.register_agent("KC", ["x"], {})
    g, h = _probe_ok()
    with g, h:
        s.set_agent_endpoint(a["id"], "https://one.example/a2a", verify=True)
        # CHANGED endpoint inside the window → full pair records
        s.set_agent_endpoint(a["id"], "https://two.example/a2a", verify=True)
        assert len(_ev(s, "endpoint_declared")) == 2
        assert len(_ev(s, "endpoint_verification")) == 2
        # window EXPIRED → repeat records again (the ≤4-pairs/day heartbeat)
        with mock.patch.object(store_mod, "KEEPALIVE_EVENT_WINDOW_S", 0.0):
            s.set_agent_endpoint(a["id"], "https://two.example/a2a", verify=True)
        assert len(_ev(s, "endpoint_declared")) == 3
        assert len(_ev(s, "endpoint_verification")) == 3


def test_unverified_redeclaration_still_records():
    s = _fresh(); a = s.register_agent("KU", ["x"], {})
    g, h = _probe_ok()
    with g, h:
        s.set_agent_endpoint(a["id"], "https://w.example/a2a", verify=True)
    # verify=False re-declare downgrades evidence — that is a state change and
    # must stay visible regardless of the window
    s.set_agent_endpoint(a["id"], "https://w.example/a2a")
    assert len(_ev(s, "endpoint_declared")) == 2
