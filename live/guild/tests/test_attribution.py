"""Honest 'genuine external agent' detection — we must not fool ourselves.

The failure this locks against: a `Python-urllib/3.10` call from our own
verification tooling being counted as the 'first genuine external agent'. Bare
tooling (curl/urllib/empty UA) with an anonymous actor is NOT genuine external;
only an attributable, non-ours identity is.
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from app.store import Store  # noqa: E402
from app.attribution import is_genuine_external, attribution_class  # noqa: E402


def test_our_own_tooling_traffic_is_not_genuine_external():
    for ua in ("Python-urllib/3.10", "curl/8.4.0", "python-requests/2.31", "",
               "Wget/1.21", "mcp/remote", "mcp:fastmcp/1.0", "mcp:agent-guild/1"):
        e = {"fp": False, "ua": ua, "key": "anon"}
        assert is_genuine_external(e) is False, ua


def test_attributable_identities_are_genuine_external():
    # a real registered billing key acting
    assert is_genuine_external({"fp": False, "ua": "", "key": "ak_realagent123"}) is True
    # an MCP client that named itself and isn't ours
    assert is_genuine_external({"fp": False, "ua": "mcp:acme-orchestrator/2.1", "key": "mcp"}) is True
    # a recognised agent-framework UA
    assert is_genuine_external({"fp": False, "ua": "langchain/0.2 python-httpx/0.27", "key": "anon"}) is True
    # first-party is never genuine external, whatever the UA
    assert is_genuine_external({"fp": True, "ua": "mcp:acme/1", "key": "ak_x"}) is False


def test_attribution_labels():
    assert attribution_class({"fp": True, "ua": "x", "key": "ak_x"}) == "first_party"
    assert attribution_class({"fp": False, "ua": "mcp:acme/1", "key": "mcp"}) == "genuine_external"
    assert attribution_class({"fp": False, "ua": "mcp/remote", "key": "mcp"}) == "unattributable_mcp"
    assert attribution_class({"fp": False, "ua": "Python-urllib/3.10", "key": "anon"}) == "tooling_or_ours"


def test_instrumentation_exposes_honest_signal():
    s = Store(path="")
    # our own verification traffic: a urllib best_agent hit, anon
    s.record_event(None, "query", ua="Python-urllib/3.10", endpoint="best_agent", paid=False)
    instr = s.instrumentation()
    # 'external' counts it (not first-party)...
    assert instr["external"]["unique_agents"] == 1
    # ...but the HONEST signal does NOT — no real agent has arrived.
    assert instr["genuine_external_detected"] is False
    assert instr["genuine_external"]["unique_agents"] == 0
    assert instr["first_genuine_external_at"] is None

    # now a genuinely external, attributable agent (a real registered key) calls
    acct = s.create_account(first_party=False)          # ak_...
    s.record_event(acct["key"], "query", ua="langchain/0.2", endpoint="best_agent", paid=False)
    instr2 = s.instrumentation()
    assert instr2["genuine_external_detected"] is True
    assert instr2["genuine_external"]["unique_agents"] == 1
    assert acct["key"] in instr2["genuine_external_actors"]


def test_recent_feed_labels_each_event():
    s = Store(path="")
    s.record_event(None, "query", ua="Python-urllib/3.10", endpoint="best_agent")
    ev = s.recent_events(limit=5, external_only=True)[0]
    assert ev["genuine_external"] is False
    assert ev["attribution"] == "tooling_or_ours"
