"""AGD-1 contract conformance of the live server + signed offline cache."""
from __future__ import annotations

import json
import urllib.request

from agentguild_trustplane.contract import validate_decision, CONTRACT_ID
from agentguild_trustplane.verify import verify_data_integrity, within_validity
from agentguild_trustplane.cache import SignedDecisionCache
from agentguild_trustplane.client import GuildClient


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=15) as r:
        return json.loads(r.read().decode())


def test_check_serves_conformant_agd1(guild_server, seeded):
    out = _get(guild_server["base"], "/check?capability=tp-echo")
    d = out["decision"]
    assert d is not None and d["contract"] == CONTRACT_ID
    assert validate_decision(d) == []
    # legacy demotion: verdict still present but marked, contract_note explains
    assert "contract_note" in out
    assert "deprecated" in (out["verdict"] or {})


def test_signed_decision_verifies_with_independent_verifier(guild_server, seeded):
    doc = _get(guild_server["base"],
               "/check?capability=tp-echo&signed=true&ttl_seconds=120")
    v = verify_data_integrity(doc)
    assert v["verified"], v
    valid, age = within_validity(doc)
    assert valid and age is not None and age < 60
    # tamper -> reject
    doc2 = json.loads(json.dumps(doc))
    doc2["decision"]["estimate"] = 0.999
    assert not verify_data_integrity(doc2)["verified"]


def test_cache_round_trip_and_tamper_rejection(guild_server, seeded, tmp_path):
    doc = _get(guild_server["base"], "/check?capability=tp-echo&signed=true")
    cache = SignedDecisionCache(tmp_path)
    assert cache.put("decision", "tp-echo", doc)
    got, state, age = cache.get("decision", "tp-echo")
    assert got is not None and state == "fresh"
    # tampered bytes never come back
    bad = json.loads(json.dumps(doc))
    bad["decision"]["estimate"] = 1.0
    assert not cache.put("decision", "evil", bad)


def test_cache_pins_issuer_tofu(guild_server, seeded, tmp_path):
    doc = _get(guild_server["base"], "/check?capability=tp-echo&signed=true")
    cache = SignedDecisionCache(tmp_path)
    cache.put("decision", "tp-echo", doc)
    assert cache.trusted_issuers == [doc["issuer"]]
    # a doc from a different issuer is refused once pinned
    cache2 = SignedDecisionCache(tmp_path / "b",
                                 trusted_issuers=["did:key:zOther"])
    assert not cache2.put("decision", "tp-echo", doc)


def test_client_outage_falls_back_to_cache(guild_server, seeded, tmp_path):
    cache = SignedDecisionCache(tmp_path)
    live = GuildClient(guild_server["base"], cache=cache)
    doc, ch, _ = live.signed_decision("tp-echo")
    assert ch == "live" and doc is not None
    dead = GuildClient("http://127.0.0.1:9", cache=cache, timeout=0.5)
    doc2, ch2, age2 = dead.signed_decision("tp-echo")
    assert ch2 == "cache" and doc2 is not None and age2 is not None
    empty = GuildClient("http://127.0.0.1:9",
                        cache=SignedDecisionCache(tmp_path / "empty"),
                        timeout=0.5)
    doc3, ch3, _ = empty.signed_decision("tp-echo")
    assert ch3 == "outage" and doc3 is None
