"""The advertised catalogue and the measurable catalogue must be ONE list.

Defect this pins (found live on 2026-08-13, release 2.3.0 / 4cf895aa):

  ``paidcatalog._OPERATIONS`` advertised six priced operations -- the manifest,
  the MCP Registry ``ai.agent-guild/paid-operations`` block and the x402 rail
  all sold ``protected_payment_decision``. ``experiments.OPERATION_EVENTS``
  listed five. ``_require_known_operation`` derives its allow-list from
  ``OPERATION_EVENTS``, so:

    GET /commercial?operation=protected_payment_decision  -> 400
    GET /funnel/paid?operation=protected_payment_decision -> 400

  while the UNSCOPED ``/funnel/paid`` was already reporting qualified external
  exposure for exactly that operation. An operation we sell, that external
  actors were reaching, could not be read at the only scope that can price it.

This is the same failure mode as the portfolio/scope disagreement: a number is
produced somewhere and cannot be reconciled anywhere. The guard is structural --
a new priced operation cannot be advertised without becoming measurable, and a
measurable operation cannot exist that we do not advertise.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import experiments, paidcatalog


def _advertised() -> set[str]:
    return {o["operation"] for o in paidcatalog._OPERATIONS}


def test_every_advertised_operation_is_measurable():
    missing = _advertised() - set(experiments.OPERATION_EVENTS)
    assert not missing, (
        "advertised for sale but absent from experiments.OPERATION_EVENTS, so "
        f"?operation= is a 400 for it: {sorted(missing)}")


def test_every_measurable_operation_is_advertised():
    extra = set(experiments.OPERATION_EVENTS) - _advertised()
    assert not extra, (
        "measurable but not in the advertised catalogue -- a price nobody can "
        f"discover: {sorted(extra)}")


def test_protected_payment_decision_is_present_and_named():
    assert "protected_payment_decision" in experiments.OPERATION_EVENTS
    assert experiments.OPERATION_EVENTS["protected_payment_decision"] == (
        "protected_payment_decision_issued",)


def test_completion_event_types_are_unique_across_operations():
    seen: dict[str, str] = {}
    for op, types in experiments.OPERATION_EVENTS.items():
        for t in types:
            assert t not in seen, (
                f"event type {t!r} counts completions for both {seen[t]!r} "
                f"and {op!r}; one operation's revenue would be read as "
                "another's")
            seen[t] = op


def test_scoped_reads_accept_every_advertised_operation():
    """The allow-list the API enforces is the advertised list, not a subset."""
    from app.main import _require_known_operation

    for op in sorted(_advertised()):
        _require_known_operation(op)


def test_unknown_operation_is_still_rejected():
    """The fix widens the allow-list; it must not remove the guard."""
    from app.main import _require_known_operation

    with pytest.raises(HTTPException) as exc:
        _require_known_operation("not_an_operation")
    assert exc.value.status_code == 400
