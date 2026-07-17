"""agent-guild/caller-proof/v1 (machine-attribution pass).

A transport-neutral signed caller envelope a machine creates and verifies
without a human: the caller's self-controlled did:key signs a JCS-canonical
payload binding DID, method/action, canonical resource, request-body hash,
issued/expiry times, unique nonce, audience "agent-guild" and the protocol
version. Verified offline with the existing Ed25519/JCS primitives; nonce
replay, expiry, audience and exact request binding are enforced; anonymous
calls stay allowed but UNVERIFIED; user-agent strings can never create
verified status.
"""
import base64
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import callerproof, crypto, demand
from app.state import store

EXT_UA = "external-agent-framework/2.0 (crewai)"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _keypair():
    priv, pub = crypto.generate_keypair()
    return priv, pub, crypto.did_from_public_key(pub)


def _cap():
    return "cp-" + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# envelope: create + offline verify, exact binding
# ---------------------------------------------------------------------------

def test_create_and_verify_roundtrip_offline():
    priv, pub, did = _keypair()
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource="/check?capability=x",
                                   body=b"")
    p = env["payload"]
    assert p["v"] == "agent-guild/caller-proof/v1"
    assert p["did"] == did and p["aud"] == "agent-guild"
    assert p["method"] == "GET" and p["resource"] == "/check?capability=x"
    assert p["body_sha256"] and p["nonce"] and p["iat"] < p["exp"]
    out = callerproof.verify_proof(store, env, method="GET",
                                   resource="/check?capability=x", body=b"")
    assert out["verified"] is True and out["did"] == did
    # pure-offline signature verification with the existing primitives
    assert crypto.verify_jcs(p, env["signature"],
                             crypto.public_key_from_did(did))


@pytest.mark.parametrize("mutate,needles", [
    # mutating a SIGNED field invalidates the signature (detected there);
    # audience/expiry are checked before the signature.
    (lambda e: e["payload"].update(method="POST"), ("signature", "binding")),
    (lambda e: e["payload"].update(resource="/other"),
     ("signature", "binding")),
    (lambda e: e["payload"].update(did="did:key:z6Mkforged"),
     ("signature", "did")),
    (lambda e: e.update(signature="ab" * 32), ("signature",)),
    (lambda e: e["payload"].update(aud="someone-else"), ("audience",)),
    (lambda e: e["payload"].update(exp=int(time.time()) - 10), ("expir",)),
])
def test_any_tamper_fails_verification(mutate, needles):
    priv, pub, did = _keypair()
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource="/check?capability=x", body=b"")
    mutate(env)
    out = callerproof.verify_proof(store, env, method="GET",
                                   resource="/check?capability=x", body=b"")
    assert out["verified"] is False
    assert any(n in out["reason"].lower() for n in needles), out["reason"]


def test_body_hash_binds_the_exact_request_body():
    priv, pub, did = _keypair()
    env = callerproof.create_proof(priv, did, method="POST",
                                   resource="/agents/register",
                                   body=b'{"name":"x"}')
    ok = callerproof.verify_proof(store, env, method="POST",
                                  resource="/agents/register",
                                  body=b'{"name":"x"}')
    assert ok["verified"] is True
    bad = callerproof.create_proof(priv, did, method="POST",
                                   resource="/agents/register",
                                   body=b'{"name":"x"}')
    out = callerproof.verify_proof(store, bad, method="POST",
                                   resource="/agents/register",
                                   body=b'{"name":"EVIL"}')
    assert out["verified"] is False and "body" in out["reason"].lower()


def test_nonce_replay_is_rejected_durably():
    priv, pub, did = _keypair()
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource="/x", body=b"")
    assert callerproof.verify_proof(store, env, method="GET", resource="/x",
                                    body=b"")["verified"] is True
    out = callerproof.verify_proof(store, env, method="GET", resource="/x",
                                   body=b"")
    assert out["verified"] is False and "nonce" in out["reason"].lower()


# ---------------------------------------------------------------------------
# transports: HTTP header + MCP _meta; anonymous stays allowed/unverified
# ---------------------------------------------------------------------------

def test_http_header_transport_marks_demand_verified():
    from app.main import app
    priv, pub, did = _keypair()
    cap = _cap()
    resource = f"/check?capability={cap}"
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource=resource, body=b"")
    hdr = base64.b64encode(json.dumps(env).encode()).decode()
    with TestClient(app) as client:
        r = client.get(resource,
                       headers={"User-Agent": EXT_UA,
                                callerproof.HTTP_HEADER: hdr})
        assert r.status_code in (200, 402)
    ev = [e for e in store.events if e.get("type") == "capability_demand"
          and e.get("capability") == cap][-1]
    assert ev.get("caller_proof_verified") is True
    assert ev.get("caller_did") == did


def test_anonymous_calls_stay_allowed_but_unverified():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r = client.get(f"/check?capability={cap}",
                       headers={"User-Agent": EXT_UA})
        assert r.status_code in (200, 402)      # anonymous is still served
    ev = [e for e in store.events if e.get("type") == "capability_demand"
          and e.get("capability") == cap][-1]
    assert not ev.get("caller_proof_verified")


def test_user_agent_can_never_create_verified_status():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        client.get(f"/check?capability={cap}",
                   headers={"User-Agent":
                            "definitely-a-real-agent/1.0 (verified)"})
    ev = [e for e in store.events if e.get("type") == "capability_demand"
          and e.get("capability") == cap][-1]
    assert not ev.get("caller_proof_verified"), (
        "a user-agent string is diagnostics, never verified machine "
        "identity")


def test_tampered_http_header_is_unverified_not_500():
    from app.main import app
    priv, pub, did = _keypair()
    cap = _cap()
    resource = f"/check?capability={cap}"
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource=resource, body=b"")
    env["payload"]["resource"] = "/somewhere-else"
    hdr = base64.b64encode(json.dumps(env).encode()).decode()
    with TestClient(app) as client:
        r = client.get(resource, headers={"User-Agent": EXT_UA,
                                          callerproof.HTTP_HEADER: hdr})
        assert r.status_code in (200, 402)
    ev = [e for e in store.events if e.get("type") == "capability_demand"
          and e.get("capability") == cap][-1]
    assert not ev.get("caller_proof_verified")


def test_mcp_meta_mapping_verifies():
    priv, pub, did = _keypair()
    tool_args = {"capability": "kr-legal"}
    env = callerproof.create_proof(
        priv, did, method="tools/call", resource="guild_check",
        body=callerproof.mcp_args_body(tool_args))
    out = callerproof.verify_proof(
        store, env, method="tools/call", resource="guild_check",
        body=callerproof.mcp_args_body(tool_args))
    assert out["verified"] is True
    assert callerproof.MCP_META_KEY == "io.agent-guild/caller-proof"


# ---------------------------------------------------------------------------
# discovery surfaces publish the schema + instructions
# ---------------------------------------------------------------------------

def test_schema_published_on_machine_discovery_surfaces():
    from app.main import app
    with TestClient(app) as client:
        manifest = client.get("/.well-known/agent-guild.json").json()
        cp = manifest.get("caller_proof")
        assert cp and cp["protocol"] == "agent-guild/caller-proof/v1"
        assert cp["http_header"] == callerproof.HTTP_HEADER
        assert cp["mcp_meta_key"] == callerproof.MCP_META_KEY
        assert "a2a_metadata_key" in cp
        doc = client.get("/caller-proof").json()
        assert doc["protocol"] == "agent-guild/caller-proof/v1"
        assert doc["payload_fields"]["nonce"]
        assert doc["example"]["payload"]["v"] == "agent-guild/caller-proof/v1"
        assert "verification" in doc
