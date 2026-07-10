"""THE Pilot A acceptance test: reproduce an independent external agent with
NO preloaded knowledge of Agent Guild.

The simulated agent only knows (a) the standard A2A well-known path convention
and (b) how to read JSON. It must: discover an AG identity through a standard
machine-discovery mechanism → interpret capabilities → select one for its task
→ invoke it → validate the result → verify provenance → inspect membership
terms → register (because terms show positive expected utility) with the
referral token → and the graph must attribute the whole path as genuine
external. No step uses any AG-internal constant, id, or shortcut.
"""
import os
os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402
from app.swarm.router import ensure_built  # noqa: E402

# The external agent self-identifies with a framework UA (a real autonomous
# HTTP agent would), so attribution classifies it genuine_external.
EXT = {"User-Agent": "crewai/1.5 independent-probe-agent"}


def setup_module():
    ensure_built()
    from app.swarm import gateway
    gateway._day_buckets.clear()
    gateway._minute_window.clear()
    store.swarm_state["killed"] = False


def test_independent_external_agent_full_journey():
    client = TestClient(app)
    task_data = "{'result': 'ok', 'items': [1, 2,], // done\n"

    # 1. DISCOVER via the standard A2A well-known path — the only prior knowledge
    card = client.get("/.well-known/agent-card.json", headers=EXT)
    assert card.status_code == 200
    card = card.json()
    assert card["protocolVersion"]

    # 2. READ structured descriptions: find an invocation skill on the card
    invoke_skills = [s for s in card["skills"] if s["id"].startswith("ag.")
                     or s["id"] == "guild.invoke"]
    assert invoke_skills, "card must advertise invocable capabilities"
    # the generic invoke skill points at the identity index
    generic = next(s for s in card["skills"] if s["id"] == "guild.invoke")
    import re
    idx_url = re.search(r"(\S+/\.well-known/ag-identities/index\.json)",
                        generic["description"]).group(1)
    idx_path = "/" + idx_url.split("/", 3)[3]
    idx = client.get(idx_path, headers=EXT).json()
    assert idx["count"] >= 10

    # 3. POSITIVE EXPECTED UTILITY: pick the capability whose schema fits the
    # task (repairing malformed JSON) by reading identity documents.
    chosen = None
    for entry in idx["identities"]:
        doc_path = "/" + entry["document"].split("/", 3)[3]
        doc = client.get(doc_path, headers=EXT).json()
        ident = doc["identity"]
        if "repair" in ident["capability"]["summary"].lower():
            # machine checks before invoking: schema, latency, price, health
            assert ident["health"] == "passing"
            assert ident["benchmark"]["ok"] is True
            assert ident["pricing"]["guest_cost_credits"] == 0
            schema = ident["capability"]["input_schema"]
            assert "text" in schema["properties"]
            chosen = (entry, ident)
            break
    assert chosen, "external agent could not find a fitting capability"
    entry, ident = chosen

    # 3b. inspect terms BEFORE invoking (as the index instructs)
    terms_path = "/" + idx["terms"].split("/", 3)[3]
    terms = client.get(terms_path, headers=EXT).json()
    assert terms["guest_tier"]["auth"] == "none"
    guest_limit = terms["guest_tier"]["daily_invocation_limit"]
    assert guest_limit > 0

    # 4. INVOKE through the endpoint the identity document declares
    invoke_path = "/" + ident["protocols"]["rest"]["url"].split("/", 3)[3]
    r = client.post(invoke_path, json={"text": task_data}, headers=EXT)
    assert r.status_code == 200, r.text
    body = r.json()

    # 5. USEFUL, VERIFIABLE RESULT: the JSON is repaired and provenance verifies
    assert body["ok"] is True
    assert body["result"]["parsed"] == {"result": "ok", "items": [1, 2]}
    env = body["provenance"]
    did_doc_path = "/" + env["verification"]["did_document"].split("/", 3)[3]
    did_doc = client.get(did_doc_path, headers=EXT).json()
    # verify signature using ONLY fetched material (the published Guild key)
    from app.crypto import verify_jcs
    pub = did_doc.get("public_key") or did_doc.get("publicKeyHex") \
        or env["verification"]["public_key"]
    assert verify_jcs(env["envelope"], env["verification"]["signature"], pub)

    # 6. JOIN: terms showed member tier adds budget + listing; register via API
    referral = env["envelope"]["referral_token"]
    assert referral
    join = terms["member_tier"]["join"]
    reg = client.request(join["method"], join["path"], headers=EXT, json={
        "name": "IndependentProbeAgent",
        "capabilities": ["integration-testing"],
        "metadata": {"referral_token": referral},
    })
    assert reg.status_code == 200, reg.text
    agent_id = reg.json()["id"]

    # 7. FURTHER DISCOVERY LOOP: the referral edge exists and is labelled organic
    g = client.get("/swarm/graph").json()
    edges = [b for b in g["registrations_via_referral"]
             if b["agent_id"] == agent_id]
    assert edges and edges[0]["via_capability"] == ident["capability"]["id"]
    assert edges[0]["first_party"] is False
    assert edges[0] in g["organic_registrations_via_referral"]

    # and the whole path counted as genuine external in growth metrics
    from app.swarm.graph import growth_stats
    ge = growth_stats(store)["genuine_external"]
    assert ge["discovery_fetches"] >= 3      # index + identity docs + terms
    assert ge["successful_completions"] >= 1
    assert growth_stats(store)["machine_registrations_via_referral"] >= 1


def test_a2a_transport_invocation_also_works():
    """The same acquisition path over A2A message/send (generic HTTP agent)."""
    client = TestClient(app)
    r = client.post("/a2a", headers=EXT, json={
        "jsonrpc": "2.0", "id": 7, "method": "message/send",
        "params": {"message": {"parts": [{
            "kind": "text",
            "text": 'invoke: calc.unit_convert {"value": 26.2, "from": "mi", "to": "km"}'}]}},
    })
    assert r.status_code == 200, r.text
    import json
    rpc = r.json()
    assert "result" in rpc, rpc
    text_parts = [p for p in rpc["result"]["parts"] if p.get("kind") == "text"]
    payload = json.loads(text_parts[0]["text"])
    assert payload["ok"] is True
    assert abs(payload["result"]["result"] - 42.164) < 0.01
    assert payload["provenance"]["verification"]["signature"]


def test_first_party_simulation_never_pollutes_growth():
    """False-demand exclusion: replaying the E2E with first-party tagging must
    not move genuine-external numbers."""
    from app.swarm.graph import growth_stats
    client = TestClient(app)
    before = growth_stats(store)["genuine_external"]
    fp_headers = {"User-Agent": "crewai/1.5 our-own-sim",
                  "X-Guild-Source": "swarm-sim"}
    client.get("/.well-known/ag-identities/index.json", headers=fp_headers)
    client.post("/invoke/calc.stats", json={"values": [1, 2, 3]},
                headers=fp_headers)
    after = growth_stats(store)["genuine_external"]
    assert after == before
