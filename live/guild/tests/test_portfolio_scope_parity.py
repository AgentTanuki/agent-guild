"""The portfolio paid-offer counter must equal the sum of its scoped parts.

WHAT WENT WRONG
  PR #47 made `qualified_exposure`'s portfolio branch able to leave zero, and
  stated the invariant it was built on:

      "Same two definitions as the scoped branch, unioned over every
       operation - deliberately NOT a looser rule, so the portfolio number and
       the sum of the scoped numbers cannot disagree."

  It IS a looser rule. The scoped branch counts a challenge only when
  ``challenged_operation == operation``; the portfolio branch counts EVERY
  ``paid_offer_shown``/``paid_offer_challenged`` event, whatever operation it
  names - including ``watch_provision`` (a real, priced-at-zero operation that
  ``/commercial`` rejects as "unknown"), an operation added later, and an event
  that carries no operation at all.

  So the headline leading indicator can read 5 while every operation a reader
  is allowed to ask about reads 0. That is production on 2026-08-06:

      /commercial                            -> paid_offers_shown = 5
      /commercial?operation=deep_preflight   -> 0
      /commercial?operation=evidence_bundle  -> 0
      /commercial?operation=watch_cycle      -> 0

  Same failure mode as the defect #47 fixed, one level up: the mandate's
  leading number is unattributable, and it fails in the FLATTERING direction -
  the portfolio looks like exposure that no operation can account for.

WHAT THIS FILE PINS
  1. The invariant itself: portfolio == sum over priced operations.
  2. Nothing is silently dropped - a challenge naming an unpriced or absent
     operation is reported under `unattributed_paid_offers_shown`, so the
     residue is visible instead of being folded into the headline.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import experiments, pricing  # noqa: E402
from app.store import Store  # noqa: E402

EXT_UA = "a2a:langchain/0.2.1"


@pytest.fixture()
def store(tmp_path) -> Store:
    pricing.load_runtime({})
    return Store(path=str(tmp_path / "guild.json"))


def _challenge(store: Store, operation, actor: str, ua: str = EXT_UA):
    kw = {} if operation is None else {"challenged_operation": operation}
    store.record_event(actor, "paid_offer_shown", ua=ua,
                       endpoint="x402_challenge", price_credits=20, **kw)


def _sum_scoped(store: Store) -> int:
    return sum(experiments.qualified_exposure(store, op)["paid_offers_shown"]
               for op in experiments.OPERATION_EVENTS)


# --------------------------------------------------------------------------
# 1. An unpriced operation must not inflate the portfolio
# --------------------------------------------------------------------------
def test_watch_provision_challenge_does_not_inflate_portfolio(store):
    """`watch_provision` is in pricing.DEFAULTS but not in OPERATION_EVENTS.

    /commercial refuses `operation=watch_provision` ("unknown operation"), so a
    reader cannot scope to it - and before this fix the portfolio counted it
    anyway, producing exposure attributable to nothing.
    """
    assert "watch_provision" in pricing.DEFAULTS
    assert "watch_provision" not in experiments.OPERATION_EVENTS

    _challenge(store, "watch_provision", "a2a:net:one")
    portfolio = experiments.qualified_exposure(store)

    assert _sum_scoped(store) == 0
    assert portfolio["paid_offers_shown"] == 0, portfolio
    # ...but it is NOT thrown away: the residue is named.
    assert portfolio["unattributed_paid_offers_shown"] == 1, portfolio


# --------------------------------------------------------------------------
# 2. A challenge with no operation at all is residue, not exposure
# --------------------------------------------------------------------------
def test_operationless_challenge_is_residue(store):
    _challenge(store, None, "a2a:net:two", "a2a:crewai/1.0")
    portfolio = experiments.qualified_exposure(store)
    assert portfolio["paid_offers_shown"] == 0, portfolio
    assert portfolio["unattributed_paid_offers_shown"] == 1, portfolio


# --------------------------------------------------------------------------
# 3. The invariant, on a mixed population
# --------------------------------------------------------------------------
def test_portfolio_equals_sum_of_scoped_on_mixed_traffic(store):
    _challenge(store, "deep_preflight", "a2a:net:a", "a2a:langchain/0.2.1")
    _challenge(store, "deep_preflight", "a2a:net:b", "a2a:crewai/1.0")
    _challenge(store, "evidence_bundle", "a2a:net:c", "a2a:autogen/0.4")
    _challenge(store, "watch_cycle", "a2a:net:d", "a2a:llamaindex/0.11")
    # residue, in three flavours
    _challenge(store, "watch_provision", "a2a:net:e", "a2a:langgraph/0.2")
    _challenge(store, None, "a2a:net:f", "a2a:semantic-kernel/1.0")
    _challenge(store, "some_operation_added_next_quarter", "a2a:net:g",
               "a2a:langchain/0.3.0")
    # our own traffic - excluded by caller class, in neither number
    _challenge(store, "deep_preflight", "a2a:net:h", "guild-live-conformance")

    portfolio = experiments.qualified_exposure(store)
    assert portfolio["paid_offers_shown"] == 4, portfolio
    assert portfolio["paid_offers_shown"] == _sum_scoped(store)
    assert portfolio["unattributed_paid_offers_shown"] == 3, portfolio


# --------------------------------------------------------------------------
# 4. The residue field is always present, so a reader can never mistake its
#    absence for zero.
# --------------------------------------------------------------------------
def test_residue_field_always_present_on_both_branches(store):
    _challenge(store, "deep_preflight", "a2a:net:a")
    assert "unattributed_paid_offers_shown" in experiments.qualified_exposure(
        store)
    scoped = experiments.qualified_exposure(store, "deep_preflight")
    assert scoped["unattributed_paid_offers_shown"] == 0, scoped


# --------------------------------------------------------------------------
# 5. The residue is NAMED, so the next read explains itself
# --------------------------------------------------------------------------
def test_residue_operations_are_named(store):
    _challenge(store, "watch_provision", "a2a:net:e", "a2a:langgraph/0.2")
    _challenge(store, "watch_provision", "a2a:net:f", "a2a:semantic-kernel/1.0")
    _challenge(store, None, "a2a:net:g", "a2a:crewai/1.0")
    portfolio = experiments.qualified_exposure(store)
    assert portfolio["unattributed_paid_offer_operations"] == {
        "watch_provision": 2, "(none)": 1}, portfolio
