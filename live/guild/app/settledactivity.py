"""Read-only wallet repetition, distinct from agent identity and useful outcomes."""
from __future__ import annotations

from collections import Counter
import re
from typing import Any, Iterable


def wallet_transaction_activity(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate already-confirmed mainnet rows after first-party exclusion.

    This does not verify a new payment or change revenue recognition. Repeated
    copies of a transaction cannot invent another payment, and a conflicting
    payer binding cannot be assigned to either wallet. Unknown or unsupported
    bindings remain explicitly outside this EVM-only measure, not outside revenue.
    No wallet, transaction identifier or customer journal is returned.
    """
    bindings: dict[tuple[str, str], set[str]] = {}
    invalid = duplicates = 0
    for record in records:
        network = str(record.get("network") or "").strip().lower()
        payer = str(record.get("payer") or "").strip().lower()
        transaction = str(record.get("transaction") or "").strip().lower()
        if not (re.fullmatch(r"eip155:[1-9][0-9]*", network)
                and re.fullmatch(r"0x[0-9a-f]{40}", payer)
                and re.fullmatch(r"0x[0-9a-f]{64}", transaction)):
            invalid += 1
            continue
        payers = bindings.setdefault((network, transaction), set())
        if payer in payers:
            duplicates += 1
        payers.add(payer)

    wallet_counts: Counter[tuple[str, str]] = Counter()
    conflicts = 0
    for (network, _transaction), payers in bindings.items():
        if len(payers) != 1:
            conflicts += 1
            continue
        wallet_counts[(network, next(iter(payers)))] += 1
    repeated = [count for count in wallet_counts.values() if count > 1]
    return {
        "version": "settled-wallet-activity-v1",
        "scope": "global_confirmed_mainnet_x402_excluding_known_first_party",
        "source": "append_only_settlement_ledger",
        "wallet_unit": "(eip155 network, case-normalized payer address)",
        "wallets_with_confirmed_transactions": len(wallet_counts),
        "wallets_with_multiple_transactions": len(repeated),
        "distinct_transactions": sum(wallet_counts.values()),
        "transactions_from_repeat_wallets": sum(repeated),
        "repeat_transactions_after_first": sum(count - 1 for count in repeated),
        "excluded_records_missing_or_invalid_binding": invalid,
        "duplicate_records_ignored": duplicates,
        "conflicting_transactions_excluded": conflicts,
        "interpretation": (
            "Wallet transaction repetition is not a count of agents, independent "
            "owners, useful outcomes or retention across time periods. Caller "
            "identity and intent remain unknown unless separately evidenced. "
            "Known first-party settlements are excluded using the same rule as "
            "revenue; unknown ownership is retained. Duplicate transaction "
            "records and ambiguous payer bindings cannot create repetition. "
            "Only valid EVM network/address/transaction bindings are counted; "
            "excluded bindings do not erase confirmed revenue. This global "
            "measure cannot promote a scoped price or product experiment."),
    }
