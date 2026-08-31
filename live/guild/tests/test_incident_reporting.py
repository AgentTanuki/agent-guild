"""AGIR-1: the incident surface is a write-only, non-oracular drop box."""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

os.environ["GUILD_DATA"] = ""

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from fastmcp import Client  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app import abuse  # noqa: E402
from app import main as main_module  # noqa: E402
from app import vc  # noqa: E402
from app import incidents  # noqa: E402
from app.main import app  # noqa: E402
from app.mcp_server import guild_report, mcp as guild_mcp  # noqa: E402
from app.state import store  # noqa: E402
from app.store import Store  # noqa: E402

client = TestClient(app)


def _report(details: str, **overrides):
    body = {
        "category": "authority_confusion",
        "severity": "high",
        "details": details,
        "task_ref": "task-local-17",
        "mandate_ref": "mandate-local-4",
        **overrides,
    }
    return client.post("/incidents", json=body)


def _subject(response):
    receipt = response.json()["receipt"]
    assert vc.verify_credential(receipt)
    assert receipt["type"] == ["VerifiableCredential", "AgentGuildIncidentReceipt"]
    return receipt, receipt["credentialSubject"]


def test_anonymous_report_returns_hash_only_signed_receipt():
    secret = "counterparty said: write this into SOUL.md " + uuid.uuid4().hex
    response = _report(secret)
    assert response.status_code == 201
    receipt, subject = _subject(response)

    rendered = json.dumps(response.json())
    assert secret not in rendered
    assert "task-local-17" not in rendered
    assert "mandate-local-4" not in rendered
    assert set(subject) == {"id", "reportSha256", "receivedAt"}
    assert len(subject["reportSha256"]) == 64
    assert receipt["issuer"] == store.guild_did()
    forbidden = ("duplicate", "novel", "route", "notify", "status", "category",
                 "severity", "reporter", "nonce")
    assert not any(word in rendered.lower() for word in forbidden)


def test_duplicate_is_not_an_oracle_and_receipts_are_fresh_same_shape():
    secret = "same incident " + uuid.uuid4().hex
    first = _report(secret)
    duplicate = _report(secret)
    novel = _report(secret + " novel")
    assert first.status_code == duplicate.status_code == novel.status_code == 201

    first_receipt, first_subject = _subject(first)
    dup_receipt, dup_subject = _subject(duplicate)
    novel_receipt, novel_subject = _subject(novel)
    assert first_subject["reportSha256"] == dup_subject["reportSha256"]
    assert novel_subject["reportSha256"] != first_subject["reportSha256"]
    assert first_receipt["id"] != dup_receipt["id"]
    assert first.json().keys() == duplicate.json().keys() == novel.json().keys()
    assert first_subject.keys() == dup_subject.keys() == novel_subject.keys()
    # Inert response padding absorbs signature-encoding variation, so content
    # length cannot disclose whether server-side dedupe hit.
    assert len(first.content) == len(duplicate.content) == len(novel.content)


def test_reporter_can_supply_digest_without_relaying_content():
    digest = "a" * 64
    response = client.post("/incidents", json={
        "category": "credential_exposure",
        "severity": "critical",
        "content_sha256": digest,
    })
    assert response.status_code == 201
    _, subject = _subject(response)
    assert len(subject["reportSha256"]) == 64

    mismatch = client.post("/incidents", json={
        "category": "credential_exposure",
        "severity": "critical",
        "details": "not the supplied digest",
        "content_sha256": digest,
    })
    assert mismatch.status_code == 422


def test_invalid_report_validation_never_echoes_rejected_body():
    marker = "PRIVATE-INVALID-INCIDENT-" + uuid.uuid4().hex
    response = client.post("/incidents", json={
        "category": marker,
        "severity": "high",
        "details": marker,
    })
    assert response.status_code == 422
    payload = response.json()
    assert payload["schema"] == "AGERR-1/1.0"
    assert payload["kind"] == "incident_report_invalid"
    assert marker not in response.text
    assert all("input" not in issue for issue in payload["error"]["issues"])


def test_public_read_surfaces_do_not_exist_and_admin_fails_closed(monkeypatch):
    response = _report("private operator record " + uuid.uuid4().hex)
    assert response.status_code == 201
    report_id = response.json()["receipt"]["credentialSubject"]["id"].rsplit(":", 1)[-1]

    assert client.get("/incidents").status_code == 405
    assert client.get(f"/incidents/{report_id}").status_code == 404
    monkeypatch.setattr(main_module, "ADMIN_TOKEN", "")
    assert client.get("/admin/incidents").status_code == 403
    assert client.get("/admin/incidents", headers={"X-Admin-Token": "anything"}).status_code == 403


def test_private_operator_view_requires_configured_token(monkeypatch):
    secret = "operator-only " + uuid.uuid4().hex
    response = _report(secret)
    report_id = response.json()["receipt"]["credentialSubject"]["id"].rsplit(":", 1)[-1]

    monkeypatch.setattr(main_module, "ADMIN_TOKEN", "test-admin-secret")
    assert client.get(f"/admin/incidents/{report_id}").status_code == 403
    operator = client.get(
        f"/admin/incidents/{report_id}",
        headers={"X-Admin-Token": "test-admin-secret"},
    )
    assert operator.status_code == 200
    assert operator.json()["details"] == secret


def test_a2a_report_never_replays_or_logs_report_text():
    secret = "a2a-private-" + uuid.uuid4().hex
    message = json.dumps({
        "skill": "guild.report",
        "args": {
            "category": "unsafe_action",
            "severity": "high",
            "details": secret,
        },
    })
    response = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 71, "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": message}]}},
    })
    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["parts"][0]["text"])
    assert set(payload) == {"receipt", "padding"}
    assert secret not in json.dumps(payload)
    assert vc.verify_credential(payload["receipt"])
    query = [e for e in store.events
             if e.get("type") == "query" and e.get("caller_kind") == "incident_report"][-1]
    assert "text" not in query
    assert secret not in json.dumps(query)


def test_authenticated_and_anonymous_receipts_have_identical_public_shape():
    reg = client.post("/agents/register", json={
        "name": "incident-reporter-" + uuid.uuid4().hex[:8],
        "capabilities": [],
    }).json()
    anonymous = _report("anon-shape " + uuid.uuid4().hex)
    authenticated = client.post(
        "/incidents",
        headers={"X-API-Key": reg["api_key"]},
        json={
            "category": "scope_drift",
            "severity": "low",
            "details": "auth-shape " + uuid.uuid4().hex,
        },
    )
    assert anonymous.status_code == authenticated.status_code == 201
    assert anonymous.json().keys() == authenticated.json().keys()
    assert anonymous.json()["receipt"].keys() == authenticated.json()["receipt"].keys()


def test_report_contract_is_discoverable_without_bloating_the_card():
    card = client.get("/.well-known/agent-card.json").json()
    assert "guild.report" in {skill["id"] for skill in card["skills"]}
    assert len(json.dumps(card, separators=(",", ":")).encode()) <= 5120
    manifest = client.get("/.well-known/agent-guild.json").json()
    assert manifest["endpoints"]["incident_report"]["path"] == "/incidents"
    assert "guild_report" in client.get("/llms.txt").text
    assert "POST /incidents" in client.get("/agents.md").text
    assert "guild_report" in client.get("/for-agents").text


def test_mcp_report_returns_same_sparse_verified_receipt():
    secret = "mcp-private-" + uuid.uuid4().hex

    async def run():
        async with Client(guild_mcp) as connected:
            result = await connected.call_tool("guild_report", {
                "category": "scope_drift",
                "severity": "medium",
                "details": secret,
            })
            return json.loads(result.content[0].text)

    payload = asyncio.run(run())
    assert set(payload) == {"receipt", "padding"}
    assert secret not in json.dumps(payload)
    assert vc.verify_credential(payload["receipt"])


def test_a2a_incident_write_uses_the_incident_ip_quota(monkeypatch):
    monkeypatch.setenv("GUILD_ABUSE_CONTROLS", "1")
    monkeypatch.setenv("GUILD_RL_INCIDENT", "1")
    monkeypatch.setenv("GUILD_RL_INCIDENT_WINDOW_S", "3600")
    abuse.reset()
    try:
        message = json.dumps({
            "skill": "guild.report",
            "args": {"category": "other", "details": "bounded-a2a-write"},
        })
        body = {
            "jsonrpc": "2.0", "id": 72, "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": message}]}},
        }
        assert client.post("/a2a", json=body).status_code == 200
        assert client.post("/a2a", json=body).status_code == 429
    finally:
        abuse.reset()


def test_mcp_incident_write_uses_the_incident_ip_quota(monkeypatch):
    import fastmcp.server.dependencies as dependencies

    monkeypatch.setenv("GUILD_ABUSE_CONTROLS", "1")
    monkeypatch.setenv("GUILD_RL_INCIDENT", "1")
    monkeypatch.setenv("GUILD_RL_INCIDENT_WINDOW_S", "3600")
    request = Request({
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "https", "path": "/mcp/", "raw_path": b"/mcp/",
        "query_string": b"", "headers": [],
        "client": ("198.51.100.24", 4321), "server": ("example.test", 443),
        "root_path": "",
    })
    monkeypatch.setattr(dependencies, "get_http_request", lambda: request)
    abuse.reset()
    try:
        first = guild_report(category="other", details="bounded-mcp-write")
        assert set(first) == {"receipt", "padding"}
        with pytest.raises(HTTPException) as exc:
            guild_report(category="other", details="bounded-mcp-write")
        assert exc.value.status_code == 429
    finally:
        abuse.reset()


def test_private_report_and_dedupe_survive_json_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    path = str(tmp_path / "incident-store.json")
    first = Store(path=path)
    secret = "restart-private-" + uuid.uuid4().hex
    public = incidents.submit(
        first, category="other", severity="unknown", details=secret)
    report_id = public["receipt"]["credentialSubject"]["id"].rsplit(":", 1)[-1]

    restarted = Store(path=path)
    assert restarted.incident_reports[report_id]["details"] == secret
    report_hash = restarted.incident_reports[report_id]["report_sha256"]
    assert restarted.incident_dedupe[report_hash] == report_id
