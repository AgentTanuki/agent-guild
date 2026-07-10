"""SSRF-safe declaration-time reachability verifier (2026-07-10).

The verifier is owner-initiated, runs ONLY at endpoint declaration, and never
from a read path. It rejects prohibited endpoint properties at declaration but
preserves a policy-valid declaration whose host is merely down. INVOCATION_
VERIFIED comes only from a guild-observed receipt, never a generic HTTP answer.
"""
import os, socket, tempfile, time
from unittest import mock

import pytest

os.environ.setdefault("GUILD_DATA", os.path.join(tempfile.mkdtemp(), "g.json"))

import app.reachability as R
from app.store import Store


def _fresh():
    import uuid
    return Store(path=os.path.join(tempfile.mkdtemp(), f"{uuid.uuid4().hex}.json"))


# --- URL policy (pure, no network) -------------------------------------------

@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a2a", True),
    ("http://example.com:8080/x", True),
    ("https://user:pass@example.com", False),   # embedded credentials
    ("ftp://example.com", False),               # scheme
    ("https://example.com:22", False),          # port
    ("http://127.0.0.1/x", False),              # loopback literal
    ("http://10.0.0.5/x", False),               # private literal
    ("http://169.254.1.1/x", False),            # link-local literal
    ("http://[::1]/x", False),                  # loopback v6
    ("http://0.0.0.0/x", False),                # unspecified
    ("http://224.0.0.1/x", False),              # multicast
    ("not-a-url", False),
])
def test_url_policy(url, ok):
    assert R.url_policy_check(url)[0] is ok


def test_declaration_rejects_prohibited_but_accepts_valid_public():
    s = _fresh()
    a = s.register_agent("Reachable", ["x"], {})
    # prohibited -> ValueError (route maps to 422)
    with pytest.raises(ValueError):
        s.set_agent_endpoint(a["id"], "http://127.0.0.1:9000")
    # valid public URL declares fine (no network, verify off)
    out = s.set_agent_endpoint(a["id"], "https://example.com/a2a")
    assert out["reachability_status"] == "declared_unverified"
    assert out["recommended_for_routing"] is False


def test_dns_resolving_to_private_is_refused_as_failure_not_rejection():
    """A public hostname that RESOLVES to a private address must fail the probe
    (currently_unreachable) — the declaration is preserved, not rejected."""
    s = _fresh()
    a = s.register_agent("Rebind", ["x"], {})
    with mock.patch.object(R.socket, "getaddrinfo",
                           return_value=[(2, 1, 6, "", ("10.1.2.3", 443))]):
        out = s.set_agent_endpoint(a["id"], "https://rebind.example/a2a", verify=True)
    assert out["reachability_status"] == "currently_unreachable"
    assert s.get_agent(a["id"])["metadata"]["endpoint"] == "https://rebind.example/a2a"


def test_probe_success_marks_recently_reachable():
    s = _fresh()
    a = s.register_agent("Live", ["x"], {})
    with mock.patch.object(R.socket, "getaddrinfo",
                           return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
         mock.patch.object(R, "_http_head_pinned", return_value=200):
        out = s.set_agent_endpoint(a["id"], "https://example.com/a2a", verify=True)
    assert out["reachability_status"] == "recently_reachable"
    assert out["recommended_for_routing"] is True
    assert out["verification_method"] == "declaration_probe"
    assert out["last_verified_at"] is not None


def test_redirect_is_a_verification_failure():
    s = _fresh()
    a = s.register_agent("Redir", ["x"], {})
    with mock.patch.object(R.socket, "getaddrinfo",
                           return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
         mock.patch.object(R, "_http_head_pinned", return_value=302):
        out = s.set_agent_endpoint(a["id"], "https://example.com/a2a", verify=True)
    assert out["reachability_status"] == "currently_unreachable"
    assert out["recommended_for_routing"] is False


def test_timeout_preserves_declaration_as_currently_unreachable():
    s = _fresh()
    a = s.register_agent("Slow", ["x"], {})
    with mock.patch.object(R.socket, "getaddrinfo",
                           return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
         mock.patch.object(R, "_http_head_pinned", side_effect=socket.timeout()):
        out = s.set_agent_endpoint(a["id"], "https://example.com/a2a", verify=True)
    assert out["reachability_status"] == "currently_unreachable"
    assert s.get_agent(a["id"])["metadata"]["endpoint"]  # declaration kept


def test_recently_reachable_expires_to_declared_unverified():
    s = _fresh()
    a = s.register_agent("Expiring", ["x"], {})
    with mock.patch.object(R.socket, "getaddrinfo",
                           return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
         mock.patch.object(R, "_http_head_pinned", return_value=200):
        s.set_agent_endpoint(a["id"], "https://example.com/a2a", verify=True)
    rec = s.get_agent(a["id"])
    # force the check timestamp beyond the TTL
    old = "2000-01-01T00:00:00+00:00"
    rec["reachability"]["checked_at"] = old
    assert R.status_for(rec["metadata"]["endpoint"], rec["reachability"]) == "declared_unverified"


def test_invocation_verified_only_from_receipt_not_probe():
    s = _fresh()
    worker = s.register_agent("Worker", ["x"], {})
    s.set_agent_endpoint(worker["id"], "https://example.com/a2a")
    # a generic probe can never yield invocation_verified
    with mock.patch.object(R.socket, "getaddrinfo",
                           return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
         mock.patch.object(R, "_http_head_pinned", return_value=200):
        out = s.set_agent_endpoint(worker["id"], "https://example.com/a2a", verify=True)
    assert out["reachability_status"] != "invocation_verified"
    # a guild-observed receipt does
    s.note_invocation_verified(worker["id"])
    rec = s.get_agent(worker["id"])
    fields = R.reachability_fields(rec["metadata"]["endpoint"], rec["reachability"])
    assert fields["reachability_status"] == "invocation_verified"
    assert fields["invocation_supported"] is True
    assert fields["recommended_for_routing"] is True


def test_note_invocation_verified_noop_without_endpoint():
    s = _fresh()
    a = s.register_agent("NoEndpoint", ["x"], {})
    s.note_invocation_verified(a["id"])   # must not raise or set anything
    assert "reachability" not in s.get_agent(a["id"])


def test_read_paths_never_probe_the_network():
    """/check, shortlist and reachability_fields must be pure. If any of them
    touched the network, this patched getaddrinfo would raise."""
    s = _fresh()
    a = s.register_agent("Readonly", ["fact-check"], {})
    s.set_agent_endpoint(a["id"], "https://example.com/a2a")
    def _boom(*args, **kw):
        raise AssertionError("read path performed DNS/network access")
    with mock.patch.object(R.socket, "getaddrinfo", side_effect=_boom):
        s.shortlist("fact-check", limit=5)     # read
        s.check("fact-check")                   # read
        R.reachability_fields("https://example.com/a2a", a.get("reachability"))
