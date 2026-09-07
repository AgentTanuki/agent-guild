"""Historical evidence must never turn credentials into public actor labels."""
import json

import pytest
from fastapi.testclient import TestClient

from app.store import Store
from app.swarm import graph


@pytest.mark.parametrize("hashing", ["0", "1"])
@pytest.mark.parametrize("field", ["actor", "key"])
def test_legacy_actor_fields_are_private_on_graph_and_dashboard(
        tmp_path, monkeypatch, hashing, field):
    from app.main import app
    from app.swarm import router
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "legacy.sqlite3"))
    monkeypatch.setenv("GUILD_HASH_KEYS", hashing)
    s = Store(path="")
    # Deliberately dummy values; insert legacy rows directly so the test does
    # not accidentally sanitise away the failure at today's write boundary.
    secrets = ["sk_fixture_old_credential_not_valid", "Bearer fixture-other-secret"]
    for raw in secrets:
        for _ in range(2):
            event = {"type": "swarm_invoke", "key": "anon", "ua": "langchain/0.3",
                     "outcome": "success", "capability": "json.repair",
                     "at": "2026-07-10T08:00:00+00:00", field: raw}
            s.backend.append_event(event)
    original = list(s.backend.iter_events())
    report = graph.build_graph(s)
    assert len(report["actors"]) == 2
    assert {row["actor"] for row in report["actors"]} == {
        graph.public_actor_id(raw) for raw in secrets}
    assert all(row["invocations"] == row["successes"] == 2 for row in report["actors"])
    assert report["actor_identifier_version"] == "swarm-actor-sha256-v1"
    monkeypatch.setattr(router, "store", s)
    client = TestClient(app)
    for path in ("/swarm/graph", "/dashboard"):
        response = client.get(path)
        assert response.status_code == 200
        assert all(raw not in response.text for raw in secrets)
    assert all(raw not in json.dumps(report) for raw in secrets)
    assert list(s.backend.iter_events(types=("swarm_invoke",))) == original
