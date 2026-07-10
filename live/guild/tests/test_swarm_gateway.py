"""Discovery swarm — acquisition gateway: guest invocation, provenance
signatures, rate limits, payload caps, kill switch, terms, referral tokens,
false-demand exclusion."""
import os
os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402
from app.crypto import verify_jcs  # noqa: E402
from app.swarm import gateway  # noqa: E402
from app.swarm.router import ensure_built  # noqa: E402

client = TestClient(app)
UA = {"User-Agent": "langchain/0.3 swarm-tests"}   # framework UA -> genuine external


def setup_module():
    ensure_built()
    gateway._day_buckets.clear()
    gateway._minute_window.clear()
    store.swarm_state["killed"] = False


def test_terms_inspectable_before_invocation():
    r = client.get("/terms.json")
    assert r.status_code == 200
    t = r.json()
    assert t["guest_tier"]["auth"] == "none"
    assert t["guest_tier"]["cost"] == "free"
    assert t["member_tier"]["join"]["human_required"] is False
    assert "no_hidden_conditions" in t["guest_tier"]


def test_guest_invocation_happy_path_with_signed_provenance():
    r = client.post("/invoke/json.repair", json={"text": "{'a': 1,}"},
                    headers=UA)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["parsed"] == {"a": 1}
    env = body["provenance"]
    # the envelope is verifiable against the Guild's own key
    assert verify_jcs(env["envelope"], env["verification"]["signature"],
                      env["verification"]["public_key"])
    assert env["envelope"]["outcome"] == "success"
    assert env["envelope"]["confidence"] == 1.0
    assert env["envelope"]["data_retention"]
    assert body["rate"]["tier"] == "guest"
    # a referral token was issued to the guest
    tok = env["envelope"]["referral_token"]
    assert tok and tok.startswith("agr_")
    assert tok in store.swarm_state["referral_tokens"]


def test_schema_violation_returns_structured_422_not_500():
    r = client.post("/invoke/json.repair", json={"wrong_field": 1}, headers=UA)
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert "input_schema" in body["result"]
    assert body["provenance"]["envelope"]["outcome"] == "error"


def test_unprocessable_payload_is_structured():
    r = client.post("/invoke/calc.unit_convert",
                    json={"value": 1, "from": "kg", "to": "s"}, headers=UA)
    assert r.status_code == 422
    assert "cannot convert" in r.json()["result"]["error"]


def test_unknown_capability_404_points_to_index():
    r = client.post("/invoke/nope.nothing", json={}, headers=UA)
    assert r.status_code == 404
    assert "index" in r.json()


def test_payload_cap_413():
    r = client.post("/invoke/json.repair",
                    json={"text": "x" * 59999 + "{}"}, headers=UA)
    # within schema maxLength but rejected if over gateway byte cap? -> under cap: fine
    big = {"text": "x" * 59000}
    r = client.post("/invoke/json.canonicalize",
                    json={"value": ["y" * 1000] * 100}, headers=UA)
    assert r.status_code in (200, 413)  # 100KB of list -> over 64KB cap
    if r.status_code == 413:
        assert r.json()["denied"] == "payload_too_large"
    else:  # ensure the cap actually triggers with a bigger payload
        r2 = client.post("/invoke/json.canonicalize",
                         json={"value": ["y" * 1000] * 200}, headers=UA)
        assert r2.status_code == 413


def test_guest_daily_budget_and_429_headers():
    original = gateway.GUEST_DAILY_LIMIT
    gateway.GUEST_DAILY_LIMIT = 3
    gateway._day_buckets.clear()
    try:
        hdrs = {"User-Agent": "crewai/1.0 budget-test"}
        for _ in range(3):
            assert client.post("/invoke/calc.stats", json={"values": [1, 2]},
                               headers=hdrs).status_code == 200
        r = client.post("/invoke/calc.stats", json={"values": [1, 2]}, headers=hdrs)
        assert r.status_code == 429
        assert r.headers.get("Retry-After")
        assert r.json()["denied"] == "daily_budget"
        assert "register" in r.json()["raise_limit"]
    finally:
        gateway.GUEST_DAILY_LIMIT = original
        gateway._day_buckets.clear()


def test_member_key_gets_member_tier():
    reg = client.post("/agents/register",
                      json={"name": "MemberBot", "capabilities": ["testing"]}).json()
    r = client.post("/invoke/calc.stats", json={"values": [1, 2, 3]},
                    headers={**UA, "X-API-Key": reg["api_key"]})
    assert r.status_code == 200
    assert r.json()["rate"]["tier"] == "member"
    # members don't get referral tokens (already joined)
    assert r.json()["provenance"]["envelope"]["referral_token"] is None


def test_kill_switch_admin_and_env():
    r = client.post("/swarm/kill", json={"reason": "test"})
    assert r.status_code == 200
    r = client.post("/invoke/json.repair", json={"text": "{}"}, headers=UA)
    assert r.status_code == 503
    assert r.json()["denied"] == "kill_switch"
    client.post("/swarm/revive")
    assert client.post("/invoke/json.repair", json={"text": "{}"},
                       headers=UA).status_code == 200
    # env override works without store access
    os.environ["GUILD_SWARM_KILL"] = "1"
    try:
        r = client.post("/invoke/json.repair", json={"text": "{}"}, headers=UA)
        assert r.status_code == 503
    finally:
        del os.environ["GUILD_SWARM_KILL"]


def test_first_party_invocations_are_excluded_from_growth():
    from app.swarm.graph import growth_stats
    before = growth_stats(store)["genuine_external"]["total_invocations"]
    # first-party tagged + tooling UA -> must never count as genuine external
    r = client.post("/invoke/calc.stats", json={"values": [1]},
                    headers={"User-Agent": "curl/8.0", "X-Guild-Source": "tests"})
    assert r.status_code == 200
    after = growth_stats(store)["genuine_external"]["total_invocations"]
    assert after == before
    # ...but genuine framework UA DOES count
    client.post("/invoke/calc.stats", json={"values": [1]}, headers=UA)
    assert growth_stats(store)["genuine_external"]["total_invocations"] == before + 1


def test_experience_records_store_shape_not_content():
    client.post("/invoke/json.repair", json={"text": "{'secret': 'hunter2'}"},
                headers=UA)
    rec = store.swarm_state["experience"][-1]
    import json as j
    blob = j.dumps(rec)
    assert "hunter2" not in blob          # never payload content
    assert rec["context_features"]["type"] == "object"
    assert rec["result"] in ("success", "failure")
    assert "chain_of_thought" not in blob
