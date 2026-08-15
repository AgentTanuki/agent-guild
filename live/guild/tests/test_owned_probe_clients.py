"""Our own shipped probes must never read as external agents.

Live regression on the 2.0.3 deployment: after PR #41 correctly demoted every
HTTP/A2A/registry surface, `/funnel/paid` still reported ONE qualified actor,
entirely under `paid_offer:mcp_tool`. The cause was in-repo and deterministic —
`live/scripts/live_contract_probe.py` announces
`clientInfo.name = "guild-live-conformance"`, so `attribution._mcp_client`
sees `mcp:guild-live-conformance`, a NAMED client. `is_genuine_external`
accepts a named MCP client as third-party unless it is declared as ours, and
this one was not in `OURS_MCP_CLIENTS` or `AG_TEST_UA_RE`. Our release-gate
probe therefore counted as external demand.

Fixed centrally (in the owned/test classifiers, not with funnel-specific
filtering) so historical impressions reclassify at read time with no event
deleted or rewritten.

The drift guard below reads the probe's ACTUAL identifiers out of the shipped
script, so renaming the probe fails here instead of silently re-opening the
hole.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from app import attribution
from app.store import Store

REPO = pathlib.Path(__file__).resolve().parents[3]
PROBE = REPO / "live" / "scripts" / "live_contract_probe.py"


def _probe_client_names() -> set[str]:
    """Every identity the live contract probe announces: its MCP
    clientInfo.name and its User-Agent header."""
    src = PROBE.read_text()
    names = set(re.findall(r'"clientInfo"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"', src))
    names |= set(re.findall(r'"User-Agent"\s*:\s*"([^"]+)"', src))
    return names


# --------------------------------------------------------------------------
# drift guard
# --------------------------------------------------------------------------
def test_the_live_probe_announces_an_identity_we_recognise_as_ours():
    names = _probe_client_names()
    assert names, f"could not read any client identity out of {PROBE}"
    for name in names:
        assert (name.lower() in attribution.OURS_MCP_CLIENTS
                or attribution.AG_TEST_UA_RE.search(name)), (
            f"the live contract probe announces {name!r}, which no owned/test "
            "classifier recognises — it will read as an external agent")


@pytest.mark.parametrize("shape", ["{name}", "mcp:{name}", "mcp:{name}/1.0"])
def test_every_probe_identity_is_excluded_on_every_transport_shape(shape):
    for name in _probe_client_names():
        ua = shape.format(name=name)
        ev = {"type": "paid_offer_served", "ua": ua, "key": "probe-actor"}
        assert attribution.caller_class(ev) == "AG_TEST", (
            f"{ua!r} classified as {attribution.caller_class(ev)}")
        assert attribution.is_genuine_external(ev) is False
        assert attribution.may_count_as_external_growth(
            attribution.caller_class(ev)) is False


# --------------------------------------------------------------------------
# the live pattern, replayed
# --------------------------------------------------------------------------
def _seed(store, ua, actor="mcp:net:probe", source="paid_offer:mcp_tool"):
    for op in ("deep_preflight", "evidence_bundle", "watch_cycle"):
        event = {"type": "paid_offer_served", "operation": op,
                 "source": source, "key": actor, "ua": ua,
                 "at": "2026-08-01T10:20:00+00:00"}
        store.events.append(event)
        if store.backend is not None:
            store.backend.append_event(event)
    return store


def test_the_mcp_client_classifier_independently_knows_the_probe_is_ours():
    """DEFENCE IN DEPTH, with real coverage.

    Two classifiers exclude this probe: `AG_TEST_UA_RE` (UA substring, fires
    first) and `OURS_MCP_CLIENTS` (exact MCP client-name set). The regex alone
    is enough today, which means the set entry would be untested redundancy —
    and untested redundancy rots. This asserts the MCP-native classifier knows
    the client on its own terms, so the hole cannot reopen if the UA regex is
    ever narrowed or restructured."""
    client = attribution._mcp_client("mcp:guild-live-conformance")
    assert client == "guild-live-conformance"
    assert client in attribution.OURS_MCP_CLIENTS, (
        "the MCP client-name classifier does not recognise our own probe")
    # and the version-suffixed form resolves to the same client name
    assert attribution._mcp_client(
        "mcp:guild-live-conformance/1.0") in attribution.OURS_MCP_CLIENTS


def test_the_live_readback_mcp_actor_no_longer_qualifies():
    """Exactly what production reported: 1 qualified actor, 3 operations, all
    under paid_offer:mcp_tool."""
    s = _seed(Store(path=""), "mcp:guild-live-conformance")
    f = s.paid_offer_funnel()
    assert f["raw_impressions"] == 3, "raw reach must be preserved"
    assert f["qualified_distinct_actors"] == 0
    assert f["measurable"] is False
    src = f["by_source"]["paid_offer:mcp_tool"]
    assert src["impressions"] == 3
    assert src["not_qualified"] == 3
    assert src["not_qualified_by_reason"] == {"ag_test": 3}


def test_a_genuinely_third_party_mcp_client_still_qualifies():
    """The exclusion must be the exact owned name, not 'MCP clients are out'."""
    s = _seed(Store(path=""), "mcp:some-third-party-client/1.2",
              actor="mcp:net:stranger")
    assert s.paid_offer_funnel()["qualified_distinct_actors"] == 1


def test_a_probe_and_a_stranger_together_yield_exactly_one_qualified_actor():
    s = Store(path="")
    _seed(s, "mcp:guild-live-conformance", actor="mcp:net:probe")
    _seed(s, "mcp:another-real-client/2.0", actor="mcp:net:stranger")
    f = s.paid_offer_funnel()
    assert f["raw_impressions"] == 6
    assert f["qualified_distinct_actors"] == 1


def test_historical_events_reclassify_without_being_rewritten():
    s = _seed(Store(path=""), "mcp:guild-live-conformance")
    before = [dict(e) for e in s.events]
    assert s.paid_offer_funnel()["qualified_distinct_actors"] == 0
    assert [dict(e) for e in s.events] == before, "events were mutated"


def test_the_exclusion_is_central_not_funnel_specific():
    """Every consumer of the shared classifier must see it, not just
    /funnel/paid — otherwise the next surface reintroduces the same bug."""
    ev = {"ua": "mcp:guild-live-conformance"}
    assert attribution.attribution_class(ev) == "ag_test"
    assert attribution.caller_class(ev) == "AG_TEST"
    assert attribution.is_genuine_external(ev) is False


def test_no_unrelated_mcp_name_was_swept_up():
    """Guard against a careless regex: near-miss names stay external."""
    for ua in ("mcp:conformance/1.0", "mcp:live-conformance/1.0",
               "mcp:guild/1.0", "mcp:guildlive/1.0",
               "mcp:some-third-party-client/1.2"):
        assert attribution.is_genuine_external({"ua": ua}) is True, ua


def test_the_known_substring_trade_off_is_stated_not_hidden():
    """HONEST LIMITATION, asserted rather than skipped.

    `AG_TEST_UA_RE` is a substring search, so a caller whose name CONTAINS
    `guild-live-conformance` is also excluded — e.g. a hypothetical
    `mcp:guild-live-conformance-competitor`. That is accepted deliberately:

      * the token is our own product-specific probe name, so a genuine third
        party choosing it as a prefix is implausible; and
      * the failure direction is conservative — it can only UNDER-count
        demand, never inflate it, which is the correct way for this guard to
        be wrong.

    `OURS_MCP_CLIENTS` is an exact-set match and does NOT have this property;
    the regex is what widens it. This test exists so the behaviour is a
    recorded decision instead of a surprise."""
    ev = {"ua": "mcp:guild-live-conformance-competitor/1.0"}
    assert attribution.is_genuine_external(ev) is False
    assert "guild-live-conformance-competitor" not in \
        attribution.OURS_MCP_CLIENTS
