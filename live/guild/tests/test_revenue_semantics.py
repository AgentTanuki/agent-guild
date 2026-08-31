"""Revenue-semantics correction (founder decision 2026-08-31).

A successfully confirmed mainnet x402 settlement into the Guild treasury IS
revenue unless the payer is positively identified as a Guild-controlled
first-party/canary wallet. Buyer identity, caller proof, wallet binding,
operation attribution and inferred intent are NOT prerequisites for
recognising revenue. Four concepts stay separate everywhere:

  1. settlement truth        — did real value settle on mainnet?
  2. first-party exclusion   — is the payer POSITIVELY known Guild-controlled?
  3. attribution             — mechanically linkable to a machine identity /
                               operation? (measured, never a gate)
  4. intent                  — never inferred, never required.

`unverified_payer` keeps meaning "identity/externality unverified" and is
never promoted to an independently attested external identity — but it no
longer means the confirmed money is not revenue. Experiment causality stays
fail-closed: an unattributed settlement counts in the GLOBAL headline yet can
promote a scoped price/offer experiment only when mechanically linked to that
operation, quoted price and experiment arm.

Historical totals are derived at READ TIME from the append-only settlement
ledger + the configured first-party wallet registry; no payment record is
rewritten.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import experiments, pricing  # noqa: E402
from app.store import Store  # noqa: E402

MAINNET = "eip155:8453"
CANARY_WALLET = "0x" + "69" * 20
WALLET_A = "0x" + "fe" * 20
WALLET_B = "0x" + "c5" * 20


@pytest.fixture()
def store(tmp_path, monkeypatch) -> Store:
    monkeypatch.delenv("GUILD_X402_FIRST_PARTY_PAYERS", raising=False)
    pricing.load_runtime({})
    return Store(path=str(tmp_path / "guild.json"))


def _settle(store: Store, payer: str, tx: str, *, amount: str = "10000",
            mainnet: bool = True, confirmed: bool = True,
            status: str = "settled_confirmed",
            attribution: str = "unverified_payer",
            first_party: bool | None = None,
            at: str = "2026-08-30T18:25:33Z") -> None:
    store.billing_log.append({
        "key": "x402", "type": "x402_payment", "endpoint": "check",
        "network": MAINNET if mainnet else "eip155:84532",
        "amount_atomic": amount, "payer": payer, "transaction": tx,
        "status": status, "mainnet": mainnet, "confirmed": confirmed,
        "payer_attribution": attribution, "first_party_payer": first_party,
        "at": at})


def _production_history(store: Store, monkeypatch) -> None:
    """The real four-transaction mainnet history, in miniature: one $0.01
    canary (wallet configured first-party AFTER it settled), two $0.01
    payments from one unverified wallet, one $0.01 from a second."""
    monkeypatch.setenv("GUILD_X402_FIRST_PARTY_PAYERS", CANARY_WALLET)
    _settle(store, CANARY_WALLET, "0x" + "01" * 32,
            at="2026-07-21T00:00:00Z")
    _settle(store, WALLET_A, "0x" + "02" * 32, at="2026-08-23T10:00:00Z")
    _settle(store, WALLET_A, "0x" + "03" * 32, at="2026-08-23T10:05:00Z")
    _settle(store, WALLET_B, "0x" + "04" * 32, at="2026-08-30T18:25:33Z")


# ---------------------------------------------------------------------------
# 1. Confirmed mainnet settlement from an unverified payer IS revenue
# ---------------------------------------------------------------------------
def test_confirmed_mainnet_unverified_payer_counts_as_revenue(store):
    _settle(store, WALLET_B, "0x" + "aa" * 32)
    h = store.settled_revenue_headline()
    assert h["gross_settled_revenue_usd"] == pytest.approx(0.01)
    assert h["known_first_party_settled_usd"] == 0.0
    assert h["external_settled_revenue_usd"] == pytest.approx(0.01)
    assert h["successful_external_payments"] == 1
    assert h["distinct_external_payer_wallets"] == 1
    # identity stays exactly as unverified as it was: not attributed, and
    # never promoted to an attested external identity.
    assert h["attributed_external_payments"] == 0
    assert h["attribution_coverage"] == 0.0


# ---------------------------------------------------------------------------
# 2. A positively-identified first-party canary remains excluded
# ---------------------------------------------------------------------------
def test_known_first_party_canary_excluded(store, monkeypatch):
    monkeypatch.setenv("GUILD_X402_FIRST_PARTY_PAYERS", CANARY_WALLET)
    _settle(store, CANARY_WALLET, "0x" + "ab" * 32)
    h = store.settled_revenue_headline()
    assert h["gross_settled_revenue_usd"] == pytest.approx(0.01)
    assert h["known_first_party_settled_usd"] == pytest.approx(0.01)
    assert h["external_settled_revenue_usd"] == 0.0
    assert h["successful_external_payments"] == 0
    assert h["distinct_external_payer_wallets"] == 0
    assert h["attribution_coverage"] is None
    # the settle-time first-party flag excludes too, independent of the
    # configured wallet registry
    _settle(store, WALLET_A, "0x" + "ac" * 32, first_party=True)
    h2 = store.settled_revenue_headline()
    assert h2["external_settled_revenue_usd"] == 0.0
    assert h2["known_first_party_settled_usd"] == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# 3. Testnet and unconfirmed settlements remain excluded
# ---------------------------------------------------------------------------
def test_testnet_and_unconfirmed_remain_excluded(store):
    _settle(store, WALLET_A, "0x" + "ad" * 32, mainnet=False,
            status="settled")                       # testnet
    _settle(store, WALLET_B, "0x" + "ae" * 32, confirmed=False,
            status="settled")                       # facilitator's word only
    h = store.settled_revenue_headline()
    assert h["gross_settled_revenue_usd"] == 0.0
    assert h["external_settled_revenue_usd"] == 0.0
    assert h["successful_external_payments"] == 0
    assert h["distinct_external_payer_wallets"] == 0


# ---------------------------------------------------------------------------
# 4. Two payments by one wallet: two payments, one wallet, right revenue
# ---------------------------------------------------------------------------
def test_two_payments_one_wallet(store):
    _settle(store, WALLET_A, "0x" + "b1" * 32)
    _settle(store, WALLET_A.upper(), "0x" + "b2" * 32)   # case-insensitive
    h = store.settled_revenue_headline()
    assert h["successful_external_payments"] == 2
    assert h["distinct_external_payer_wallets"] == 1
    assert h["external_settled_revenue_usd"] == pytest.approx(0.02)
    assert h["gross_settled_revenue_usd"] == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# 5. The present four-transaction history: $0.03 external after the canary
# ---------------------------------------------------------------------------
def test_present_four_transaction_history(store, monkeypatch):
    _production_history(store, monkeypatch)
    h = store.settled_revenue_headline()
    assert h["gross_settled_revenue_usd"] == pytest.approx(0.04)
    assert h["known_first_party_settled_usd"] == pytest.approx(0.01)
    assert h["external_settled_revenue_usd"] == pytest.approx(0.03)
    assert h["successful_external_payments"] == 3
    assert h["distinct_external_payer_wallets"] == 2
    assert h["attributed_external_payments"] == 0
    assert h["attribution_coverage"] == 0.0
    # append-only history untouched: labels still say unverified_payer
    assert all(b["payer_attribution"] == "unverified_payer"
               for b in store.billing_log)


def test_all_revenue_surfaces_agree_on_history(store, monkeypatch):
    """/billing/revenue (escrow_summary), /commercial (commercial_metrics
    global) and the health vector (/self-eval) must all derive the same
    figures from the same ledger at read time."""
    _production_history(store, monkeypatch)
    keys = ("gross_settled_revenue_usd", "known_first_party_settled_usd",
            "external_settled_revenue_usd", "successful_external_payments",
            "distinct_external_payer_wallets", "attributed_external_payments",
            "attribution_coverage")
    headline = store.settled_revenue_headline()
    real = store.escrow_summary()["real_settlement"]
    commercial = experiments.commercial_metrics(store)
    health = store.compute_health(persist=False)
    for k in keys:
        assert real[k] == headline[k], k
        assert commercial[k] == headline[k], k
        assert health[k] == headline[k], k
    # the health verdict quotes revenue from the headline number
    assert "$0.00" not in health["verdict"]
    # the commercial report separates the attribution gap INSIDE revenue
    assert commercial["settled_but_not_attributable_external"] == 3
    assert commercial["external_settled_revenue_usd"] == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# 6. Unattributed global revenue can never promote an unrelated experiment
# ---------------------------------------------------------------------------
def test_unattributed_global_revenue_cannot_promote_unrelated_scope(
        store, monkeypatch):
    _production_history(store, monkeypatch)
    # Global headline sees the money...
    assert experiments.commercial_metrics(store)[
        "external_settled_revenue_usd"] == pytest.approx(0.03)
    # ...but a deep_preflight-scoped view has NO event mechanically linking
    # any of these settlements to that operation, price or window.
    scoped = experiments.commercial_metrics(store, "deep_preflight")
    assert scoped["external_settled_revenue_usd"] == 0.0
    assert scoped["paid_decisions"] == 0
    assert scoped["successful_external_payments"] == 0

    # A settled completion OF ANOTHER OPERATION never leaks into the scope.
    store.record_event("mcp:net:payerx", "evidence_bundle_issued",
                       ua="mcp:someclient/1.0", endpoint="evidence_bundle",
                       paid=True, settlement_mode="x402",
                       settlement_confirmed=True, settlement_mainnet=True,
                       settlement_amount_atomic="10000",
                       payer_attribution="unverified_payer",
                       price_credits=20)
    scoped = experiments.commercial_metrics(store, "deep_preflight")
    assert scoped["external_settled_revenue_usd"] == 0.0

    # Same operation but the WRONG quoted price: excluded from that arm.
    store.record_event("mcp:net:payerx", "deep_preflight_run",
                       ua="mcp:someclient/1.0", endpoint="deep_preflight",
                       paid=True, settlement_mode="x402",
                       settlement_confirmed=True, settlement_mainnet=True,
                       settlement_amount_atomic="10000",
                       payer_attribution="unverified_payer",
                       price_credits=20)
    arm = experiments.commercial_metrics(store, "deep_preflight",
                                         tested_price_credits=10)
    assert arm["external_settled_revenue_usd"] == 0.0


def test_mechanically_linked_unverified_settlement_promotes_its_own_scope(
        store):
    """The counterpart: a confirmed mainnet settlement WITH the mechanical
    link (operation event + quoted price) counts for that scope even though
    the payer identity is unverified — intent is never required."""
    store.record_event("mcp:net:payerx", "deep_preflight_run",
                       ua="mcp:someclient/1.0", endpoint="deep_preflight",
                       paid=True, settlement_mode="x402",
                       settlement_confirmed=True, settlement_mainnet=True,
                       settlement_amount_atomic="10000",
                       payer_attribution="unverified_payer",
                       price_credits=20)
    arm = experiments.commercial_metrics(store, "deep_preflight",
                                         tested_price_credits=20)
    assert arm["external_settled_revenue_usd"] == pytest.approx(0.01)
    assert arm["paid_decisions"] == 1
    assert arm["attributed_external_payments"] == 0
    assert arm["settled_but_not_attributable_external"] == 1


def test_scoped_first_party_canary_event_still_excluded(store, monkeypatch):
    """First-party exclusion holds at event scope too — a canary completion
    is never revenue, whatever the operation link says."""
    monkeypatch.setenv("GUILD_X402_FIRST_PARTY_PAYERS", CANARY_WALLET)
    store.record_event("ag:canary", "deep_preflight_run",
                       ua="guild-canary/1.0", endpoint="deep_preflight",
                       paid=True, settlement_mode="x402",
                       settlement_confirmed=True, settlement_mainnet=True,
                       settlement_amount_atomic="10000",
                       payer_attribution="unverified_payer",
                       first_party_payer=True, price_credits=20)
    arm = experiments.commercial_metrics(store, "deep_preflight",
                                         tested_price_credits=20)
    assert arm["external_settled_revenue_usd"] == 0.0
    assert arm["known_first_party_settled_usd"] == pytest.approx(0.01)
    assert arm["paid_decisions"] == 0


def test_unverified_is_never_promoted_to_attested_external(store, monkeypatch):
    """The identity ladder is untouched: recognising the money does not move
    anyone up it."""
    _production_history(store, monkeypatch)
    att = store.escrow_summary()["real_settlement"]["attribution"]
    assert att["independently_attested_external_machine"]["transactions"] == 0
    assert att["unverified_payer"]["transactions"] == 3
    assert att["verified_first_party_canary"]["transactions"] == 1
    # and the attested-only line stays a separate, stricter claim at $0
    health = store.compute_health(persist=False)
    assert health["verified_external_revenue_usd"] == 0.0
    assert health["external_settled_revenue_usd"] == pytest.approx(0.03)
