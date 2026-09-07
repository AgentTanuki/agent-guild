"""Repeat settlement evidence survives missing caller identity without inventing adoption."""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import experiments, main  # noqa: E402
from app.store import Store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

WALLET_A = "0x" + "ab" * 20
WALLET_B = "0x" + "cd" * 20


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("GUILD_X402_FIRST_PARTY_PAYERS", raising=False)
    return Store(path=str(tmp_path / "guild.json"))


def settle(store, number, payer=WALLET_A, **overrides):
    record = {"key": "x402", "type": "x402_payment", "endpoint": "check",
              "network": "eip155:8453", "amount_atomic": "10000", "payer": payer,
              "transaction": "0x" + format(number, "064x"), "status": "settled_confirmed",
              "mainnet": True, "confirmed": True, "payer_attribution": "unverified_payer",
              "at": "2026-09-07T00:00:00+00:00"}
    record.update(overrides)
    store.billing_log.append(record)
    return record


def activity(store):
    return store.settled_revenue_headline()["settled_payer_activity"]


def test_anonymous_repeat_wallet_is_visible_without_promoting_identity(store):
    settle(store, 1)
    settle(store, 2, WALLET_A.upper())
    settle(store, 3, WALLET_B)
    before = copy.deepcopy(store.billing_log)
    observed = activity(store)
    assert observed["wallets_with_confirmed_transactions"] == 2
    assert observed["wallets_with_multiple_transactions"] == 1
    assert observed["distinct_transactions"] == 3
    assert observed["transactions_from_repeat_wallets"] == 2
    assert observed["repeat_transactions_after_first"] == 1
    headline = store.settled_revenue_headline()
    assert headline["external_settled_revenue_usd"] == 0.03
    assert headline["attributed_external_payments"] == 0
    commercial = experiments.commercial_metrics(store)
    assert commercial["repeat_paid_callers"] == 0
    assert commercial["settled_payer_activity"] == observed
    assert store.escrow_summary()["real_settlement"]["settled_payer_activity"] == observed
    assert store.billing_log == before
    assert WALLET_A not in json.dumps(observed) and WALLET_B not in json.dumps(observed)


def test_repeated_ledger_record_is_not_a_returning_payment(store):
    record = settle(store, 10)
    store.billing_log.append({**record, "transaction": record["transaction"].upper(),
                              "payer": WALLET_A.upper()})
    result = activity(store)
    assert result["wallets_with_multiple_transactions"] == 0
    assert result["distinct_transactions"] == 1
    assert result["duplicate_records_ignored"] == 1


def test_network_is_part_of_both_wallet_and_transaction_binding(store):
    settle(store, 20)
    settle(store, 20, network="eip155:1")
    result = activity(store)
    assert result["wallets_with_confirmed_transactions"] == 2
    assert result["distinct_transactions"] == 2
    assert result["wallets_with_multiple_transactions"] == 0


def test_conflicting_payer_for_same_transaction_cannot_create_repetition(store):
    settle(store, 30)
    settle(store, 30, WALLET_B)
    settle(store, 31)
    result = activity(store)
    assert result["conflicting_transactions_excluded"] == 1
    assert result["distinct_transactions"] == 1
    assert result["wallets_with_multiple_transactions"] == 0


def test_known_first_party_and_unconfirmed_records_are_excluded(store, monkeypatch):
    settle(store, 40)
    settle(store, 41)
    monkeypatch.setenv("GUILD_X402_FIRST_PARTY_PAYERS", WALLET_A)
    settle(store, 42, WALLET_B, first_party_payer=True)
    settle(store, 43, WALLET_B, confirmed=False)
    settle(store, 44, WALLET_B, mainnet=False)
    settle(store, 45, WALLET_B, status="settled_unconfirmed")
    result = activity(store)
    assert result["distinct_transactions"] == 0
    assert result["wallets_with_multiple_transactions"] == 0
    assert result["excluded_records_missing_or_invalid_binding"] == 0


@pytest.mark.parametrize("override", [
    {"payer": ""}, {"payer": "anonymous"}, {"network": ""},
    {"network": "solana:mainnet"}, {"transaction": "unverifiable-tx"},
])
def test_incomplete_binding_is_disclosed_without_erasing_revenue(store, override):
    settle(store, 50, **override)
    result = activity(store)
    assert result["distinct_transactions"] == 0
    assert result["excluded_records_missing_or_invalid_binding"] == 1
    assert store.settled_revenue_headline()["external_settled_revenue_usd"] == 0.01


def test_global_repeat_wallets_do_not_enter_a_pricing_experiment(store):
    settle(store, 60)
    settle(store, 61)
    assert activity(store)["wallets_with_multiple_transactions"] == 1
    for kwargs in ({"operation": "best_agent"}, {"since": "2026-09-01"},
                   {"tested_price_credits": 10}):
        scoped = experiments.commercial_metrics(store, **kwargs)
        assert "settled_payer_activity" not in scoped
        assert scoped["paid_decisions"] == 0


def test_public_revenue_and_commercial_routes_disclose_same_activity(store, monkeypatch):
    settle(store, 70)
    settle(store, 71)
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)
    revenue = client.get("/billing/revenue")
    commercial = client.get("/commercial")
    assert revenue.status_code == commercial.status_code == 200
    result = revenue.json()["real_settlement"]["settled_payer_activity"]
    assert commercial.json()["revenue_first"]["settled_payer_activity"] == result
    assert result["wallets_with_multiple_transactions"] == 1
    assert "not a count of agents" in result["interpretation"]
