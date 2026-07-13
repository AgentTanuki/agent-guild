"""The machine market loop: signed offers → acceptance → delivery → settlement,
plus autonomous dispute resolution (2026-07-13).

Design commitments:

* **Every record is signed and bound.** Offer and acceptance cores carry agent
  DIDs, behavioral-configuration hashes, capability, value-at-risk tier and
  deadlines; delivery receipts bind invocation ids (Guild-observed) where they
  exist. Custodial agents authenticate with their key and the Guild signs with
  the custodial key it holds for them; self-sovereign agents supply their own
  ed25519 signature over the JCS core. Either way, `*_sig` verifies against the
  named DID.

* **No permanent disputed limbo.** Disputed escrows open a CASE with a vote
  deadline; bonded machine adjudicators drawn from INDEPENDENT trust clusters
  vote; quorum majority decides; the minority's bond is slashed; each party may
  appeal once (bigger panel, final); if no quorum by the deadline the fallback
  is DETERMINISTIC: a worker-authenticated, content-addressed delivery releases
  payment, otherwise the payer is refunded. The Guild supplies rails and counts
  votes — it never judges the merits.

* **Deterministic timeouts everywhere.** Unaccepted offers expire (escrow
  refunded); accepted-but-undelivered tasks past deadline refund; delivered
  tasks the payer ignores auto-settle after a grace window. `sweep()` applies
  all of these; it is idempotent and callable by ANYONE (POST /market/sweep) —
  liveness must not depend on the Guild's scheduler.

Value-at-risk tiers (credits are the SANDBOX unit; see x402.py for real rails):
  micro < 10, low < 100, medium < 1000, high >= 1000.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from .crypto import canonicalize_jcs, sign_jcs, verify_jcs, public_key_from_did

SANDBOX_CURRENCY = "credits_sandbox"   # explicitly labelled: NOT real money


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def value_tier(amount: float) -> str:
    if amount < 10:
        return "micro"
    if amount < 100:
        return "low"
    if amount < 1000:
        return "medium"
    return "high"


def _core_hash(core: dict[str, Any]) -> str:
    return hashlib.sha256(canonicalize_jcs(core).encode("utf-8")).hexdigest()


def _sign_as(store, agent: dict[str, Any], core: dict[str, Any],
             provided_sig: Optional[str]) -> str:
    """Produce/verify the party's signature over `core`. Custodial: the Guild
    signs with the custodial key AFTER the caller authenticated as that agent.
    Self-sovereign: the caller MUST supply a signature that verifies against
    the agent's DID. Returns the hex signature; raises ValueError otherwise."""
    if agent.get("custodial") and agent.get("private_key"):
        return sign_jcs(core, agent["private_key"])
    if not provided_sig:
        raise ValueError(f"{agent['id']} is self-sovereign: supply a signature "
                         "over the JCS core")
    if not verify_jcs(core, provided_sig, public_key_from_did(agent["did"])):
        raise ValueError("signature does not verify against the agent's DID")
    return provided_sig


# --------------------------------------------------------------------------
# Offers
# --------------------------------------------------------------------------

def create_offer(store, requester: dict[str, Any], worker_id: str,
                 capability: str, amount: float, deadline_seconds: int,
                 terms: Optional[dict[str, Any]] = None,
                 requester_key: Optional[str] = None,
                 offer_signature: Optional[str] = None) -> dict[str, Any]:
    worker = store.get_agent(worker_id)
    if worker is None:
        raise ValueError("worker not found")
    if worker_id == requester["id"]:
        raise ValueError("cannot offer a task to yourself")
    deadline_seconds = max(1, min(int(deadline_seconds), 30 * 86400))
    amount = float(amount)
    offer_id = "off_" + secrets.token_hex(8)
    core = {
        "offer_id": offer_id,
        "requester_id": requester["id"], "requester_did": requester.get("did", ""),
        "worker_id": worker_id, "worker_did": worker.get("did", ""),
        "capability": capability,
        "amount": amount, "currency": SANDBOX_CURRENCY,
        "value_tier": value_tier(amount),
        "requester_config_hash": store._config_stamp(requester["id"]) or "none_declared",
        "worker_config_hash": store._config_stamp(worker_id) or "none_declared",
        "created_at": _iso(_now()),
        "deadline_at": _iso(_now() + timedelta(seconds=deadline_seconds)),
        "terms": terms or {},
    }
    sig = _sign_as(store, requester, core, offer_signature)
    escrow_id = None
    if amount > 0:
        if not requester_key:
            raise ValueError("funded offers require the requester's billing key")
        esc = store.open_escrow(requester_key, worker_id, int(amount), capability,
                                metadata={"offer_id": offer_id})
        escrow_id = esc["id"]
    offer = {
        "id": offer_id, "core": core, "core_hash": _core_hash(core),
        "offer_sig": sig, "status": "open", "escrow_id": escrow_id,
        "accept": None, "task_id": None,
    }
    with store.lock, store._txn():
        store.__dict__.setdefault("offers", {})[offer_id] = offer
        if store.backend is not None:
            store._persist_kv("offers", store.offers)
        store._save()
    store.append_ledger_event("task_created", {
        "kind": "offer", "offer_id": offer_id, "offer_hash": offer["core_hash"],
        "requester_id": requester["id"], "worker_id": worker_id,
        "capability": capability, "amount": amount, "currency": SANDBOX_CURRENCY,
        "value_tier": core["value_tier"], "escrow_id": escrow_id,
        "task_id": None,
    }, actor_did=requester.get("did", ""))
    return offer


def accept_offer(store, offer_id: str, worker: dict[str, Any],
                 accept_signature: Optional[str] = None) -> dict[str, Any]:
    offers = store.__dict__.setdefault("offers", {})
    offer = offers.get(offer_id)
    if offer is None:
        raise ValueError("offer not found")
    sweep(store)
    if offer["status"] != "open":
        raise ValueError(f"offer is {offer['status']}, not open")
    if worker["id"] != offer["core"]["worker_id"]:
        raise ValueError("only the named worker may accept this offer")
    accept_core = {
        "offer_id": offer_id,
        "offer_hash": offer["core_hash"],
        "worker_did": worker.get("did", ""),
        "worker_config_hash": store._config_stamp(worker["id"]) or "none_declared",
        "accepted_at": _iso(_now()),
    }
    sig = _sign_as(store, worker, accept_core, accept_signature)
    task = store.create_task(
        offer["core"]["requester_id"], worker["id"], offer["core"]["capability"],
        payment=offer["core"]["amount"],
        metadata={
            "offer_id": offer_id, "offer_hash": offer["core_hash"],
            "value_tier": offer["core"]["value_tier"],
            "deadline_at": offer["core"]["deadline_at"],
            "escrow_id": offer["escrow_id"],
        })
    with store.lock, store._txn():
        offer["status"] = "accepted"
        offer["accept"] = {"core": accept_core, "accept_sig": sig}
        offer["task_id"] = task["id"]
        if offer["escrow_id"]:
            esc = store.escrows.get(offer["escrow_id"])
            if esc is not None:
                esc["task_id"] = task["id"]
                if store.backend is not None:
                    store._persist_escrow(esc)
        if store.backend is not None:
            store._persist_kv("offers", store.offers)
        store._save()
    store.append_ledger_event("receipt", {
        "kind": "offer_accepted", "offer_id": offer_id,
        "offer_hash": offer["core_hash"], "accept_hash": _core_hash(accept_core),
        "task_id": task["id"], "worker_id": worker["id"],
        "value_tier": offer["core"]["value_tier"],
        "deliverable_hash": None, "outcome": "accepted_offer",
        "requester_id": offer["core"]["requester_id"],
        "task_type": offer["core"]["capability"],
        "payment": offer["core"]["amount"], "receipt_auth": "worker_key",
    }, actor_did=worker.get("did", ""))
    return offer


# --------------------------------------------------------------------------
# Disputes: bonded machine adjudication
# --------------------------------------------------------------------------

MIN_BOND = int(os.environ.get("GUILD_ADJUDICATOR_MIN_BOND", 20))
SLASH_FRACTION = 0.5                     # minority voters lose this bond share
PANEL_SIZE = 3
APPEAL_PANEL_SIZE = 5


def _window_s() -> int:
    return int(os.environ.get("GUILD_DISPUTE_WINDOW_S", 86400))


def _grace_s() -> int:
    return int(os.environ.get("GUILD_SETTLE_GRACE_S", 172800))  # 48h after deadline


def enroll_adjudicator(store, agent: dict[str, Any], key: str,
                       bond: int) -> dict[str, Any]:
    if bond < MIN_BOND:
        raise ValueError(f"minimum adjudicator bond is {MIN_BOND} {SANDBOX_CURRENCY}")
    if not agent.get("proof_of_conduct"):
        raise ValueError("adjudicators must hold a live proof_of_conduct "
                         "(POST /agents/{id}/prove first)")
    store.charge(key, int(bond), "adjudicator_bond")   # bond locked (sandbox credits)
    reg = store.__dict__.setdefault("adjudicators", {})
    with store.lock, store._txn():
        reg[agent["id"]] = {"agent_id": agent["id"], "bond": int(bond),
                            "enrolled_at": _iso(_now()), "cases": 0,
                            "slashed": 0, "active": True}
        if store.backend is not None:
            store._persist_kv("adjudicators", reg)
        store._save()
    return reg[agent["id"]]


def _clusters(store) -> dict[str, int]:
    """agent_id -> collusion-component index (independent-cluster selection)."""
    try:
        from .collusion import detect_collusion
        flags = detect_collusion(store.agents, store.attestations, store.tasks)
        comp: dict[str, int] = {}
        for i, f in enumerate(getattr(flags, "components", []) or []):
            for aid in f:
                comp[aid] = i
        return comp
    except Exception:
        return {}


def select_panel(store, case_id: str, parties: set[str], size: int,
                 exclude: Optional[set[str]] = None) -> list[str]:
    """Deterministic, independence-aware panel selection: eligible bonded
    adjudicators, excluding the parties, prior panellists and (where cluster
    data exists) anyone in the same trust cluster as a party; ranked by
    sha256(case_id || agent_id) so selection is verifiable by anyone."""
    reg = store.__dict__.get("adjudicators", {}) or {}
    comp = _clusters(store)
    party_clusters = {comp[p] for p in parties if p in comp}
    exclude = exclude or set()
    eligible = []
    for aid, rec in reg.items():
        if not rec.get("active") or rec.get("bond", 0) < MIN_BOND:
            continue
        if aid in parties or aid in exclude:
            continue
        if aid in comp and comp[aid] in party_clusters:
            continue   # same trust cluster as a party — not independent
        eligible.append(aid)
    eligible.sort(key=lambda aid: hashlib.sha256(
        (case_id + aid).encode()).hexdigest())
    return eligible[:size]


def open_case(store, escrow_id: str, raised_by: str, grounds: str) -> dict[str, Any]:
    esc = store.escrows.get(escrow_id)
    if esc is None:
        raise ValueError("escrow not found")
    cases = store.__dict__.setdefault("dispute_cases", {})
    for c in cases.values():
        if c["escrow_id"] == escrow_id and c["status"] == "open":
            return c
    case_id = "case_" + secrets.token_hex(6)
    parties = {esc["requester_id"], esc["worker_id"]} - {None, ""}
    panel = select_panel(store, case_id, parties, PANEL_SIZE)
    case = {
        "id": case_id, "escrow_id": escrow_id, "task_id": esc.get("task_id"),
        "parties": sorted(parties), "raised_by": raised_by, "grounds": grounds,
        "panel": panel, "votes": {}, "status": "open",
        "round": 1, "appealed_by": None,
        "opened_at": _iso(_now()),
        "vote_deadline_at": _iso(_now() + timedelta(seconds=_window_s())),
        "resolution": None,
    }
    with store.lock, store._txn():
        cases[case_id] = case
        if store.backend is not None:
            store._persist_kv("dispute_cases", cases)
        store._save()
    store.append_ledger_event("escrow_event", {
        "event": "dispute_case_opened", "case_id": case_id,
        "escrow_id": escrow_id, "panel": panel,
        "vote_deadline_at": case["vote_deadline_at"], "round": 1,
    }, actor_did=(store.agents.get(raised_by) or {}).get("did", ""))
    return case


def cast_vote(store, case_id: str, adjudicator: dict[str, Any], verdict: str,
              rationale: str = "", vote_signature: Optional[str] = None
              ) -> dict[str, Any]:
    if verdict not in ("release", "refund"):
        raise ValueError("verdict must be release | refund")
    cases = store.__dict__.get("dispute_cases", {}) or {}
    case = cases.get(case_id)
    if case is None:
        raise ValueError("case not found")
    if case["status"] != "open":
        raise ValueError(f"case is {case['status']}")
    if adjudicator["id"] not in case["panel"]:
        raise ValueError("you are not on this case's panel")
    if adjudicator["id"] in case["votes"]:
        raise ValueError("panel members vote exactly once")
    core = {"case_id": case_id, "adjudicator_did": adjudicator.get("did", ""),
            "verdict": verdict,
            "rationale_sha256": hashlib.sha256(rationale.encode()).hexdigest(),
            "voted_at": _iso(_now())}
    sig = _sign_as(store, adjudicator, core, vote_signature)
    with store.lock, store._txn():
        case["votes"][adjudicator["id"]] = {"core": core, "sig": sig,
                                            "verdict": verdict}
        if store.backend is not None:
            store._persist_kv("dispute_cases", cases)
        store._save()
    store.append_ledger_event("escrow_event", {
        "event": "dispute_vote", "case_id": case_id,
        "adjudicator_id": adjudicator["id"], "verdict": verdict,
        "vote_hash": _core_hash(core),
    }, actor_did=adjudicator.get("did", ""))
    maybe_resolve(store, case_id)
    return cases[case_id]


def _slash_minority(store, case: dict[str, Any], winning: str) -> list[dict[str, Any]]:
    reg = store.__dict__.get("adjudicators", {}) or {}
    slashes = []
    for aid, v in case["votes"].items():
        if v["verdict"] != winning and aid in reg:
            cut = int(reg[aid]["bond"] * SLASH_FRACTION)
            reg[aid]["bond"] -= cut
            reg[aid]["slashed"] += cut
            if reg[aid]["bond"] < MIN_BOND:
                reg[aid]["active"] = False
            slashes.append({"adjudicator_id": aid, "slashed": cut,
                            "bond_after": reg[aid]["bond"],
                            "active": reg[aid]["active"]})
    if store.backend is not None and slashes:
        store._persist_kv("adjudicators", reg)
    return slashes


def _execute(store, case: dict[str, Any], verdict: str, method: str) -> None:
    """Apply a decision to the escrow. The Guild executes the panel's (or the
    deterministic rule's) decision — it never chooses the verdict itself."""
    esc = store.escrows.get(case["escrow_id"])
    slashes = _slash_minority(store, case, verdict) if case["votes"] else []
    outcome: dict[str, Any] = {"verdict": verdict, "method": method,
                               "slashes": slashes, "decided_at": _iso(_now()),
                               "round": case["round"]}
    if esc is not None and esc.get("status") == "disputed":
        with store.lock, store._txn():
            esc["status"] = "funded"       # rearm so the normal paths can settle
            if store.backend is not None:
                store._persist_escrow(esc)
        if verdict == "release":
            try:
                store.release_escrow(case["escrow_id"], esc["requester_key"],
                                     rating=0.5)
            except ValueError as e:
                outcome["execution_error"] = str(e)
        else:
            try:
                store.refund_escrow(case["escrow_id"], esc["requester_key"])
            except ValueError as e:
                outcome["execution_error"] = str(e)
    case["status"] = "resolved"
    case["resolution"] = outcome
    cases = store.__dict__.get("dispute_cases", {})
    with store.lock, store._txn():
        if store.backend is not None:
            store._persist_kv("dispute_cases", cases)
        store._save()
    store.append_ledger_event("escrow_event", {
        "event": "dispute_resolved", "case_id": case["id"],
        "escrow_id": case["escrow_id"], "verdict": verdict, "method": method,
        "votes": {a: v["verdict"] for a, v in case["votes"].items()},
        "slashes": slashes, "round": case["round"],
    }, actor_did="")


def maybe_resolve(store, case_id: str) -> Optional[dict[str, Any]]:
    cases = store.__dict__.get("dispute_cases", {}) or {}
    case = cases.get(case_id)
    if case is None or case["status"] != "open":
        return case
    votes = [v["verdict"] for v in case["votes"].values()]
    n_panel = len(case["panel"])
    majority_needed = n_panel // 2 + 1
    for verdict in ("release", "refund"):
        if votes.count(verdict) >= majority_needed:
            _execute(store, case, verdict, method="adjudicator_quorum")
            return case
    deadline = datetime.fromisoformat(case["vote_deadline_at"])
    if _now() >= deadline:
        if votes and votes.count("release") != votes.count("refund"):
            verdict = ("release" if votes.count("release") > votes.count("refund")
                       else "refund")
            _execute(store, case, verdict, method="partial_quorum_at_deadline")
            return case
        # DETERMINISTIC fallback: worker-authenticated, content-addressed
        # delivery releases; otherwise refund. A rule, not a judgment.
        task = store.tasks.get(case.get("task_id") or "")
        delivered = bool(task and task.get("deliverable_hash")
                         and (task.get("metadata") or {}).get("receipt_auth")
                         in ("worker_key", "worker_signature"))
        _execute(store, case, "release" if delivered else "refund",
                 method="deterministic_timeout")
    return case


def appeal(store, case_id: str, party: dict[str, Any]) -> dict[str, Any]:
    cases = store.__dict__.get("dispute_cases", {}) or {}
    case = cases.get(case_id)
    if case is None:
        raise ValueError("case not found")
    if case["status"] != "resolved":
        raise ValueError("only resolved cases can be appealed")
    if case["round"] >= 2:
        raise ValueError("appeal limit reached: one appeal per dispute, final")
    if party["id"] not in case["parties"]:
        raise ValueError("only a party may appeal")
    if case["appealed_by"]:
        raise ValueError("already appealed")
    esc = store.escrows.get(case["escrow_id"])
    if esc is None:
        raise ValueError("escrow gone")
    # re-open the escrow as disputed for the appeal round
    with store.lock, store._txn():
        esc["status"] = "disputed"
        if store.backend is not None:
            store._persist_escrow(esc)
    new_panel = select_panel(store, case_id + ":appeal", set(case["parties"]),
                             APPEAL_PANEL_SIZE, exclude=set(case["panel"]))
    with store.lock, store._txn():
        case["status"] = "open"
        case["round"] = 2
        case["appealed_by"] = party["id"]
        case["panel"] = new_panel
        case["votes"] = {}
        case["vote_deadline_at"] = _iso(_now() + timedelta(seconds=_window_s()))
        if store.backend is not None:
            store._persist_kv("dispute_cases", cases)
        store._save()
    store.append_ledger_event("escrow_event", {
        "event": "dispute_appealed", "case_id": case_id,
        "escrow_id": case["escrow_id"], "by": party["id"],
        "panel": new_panel, "round": 2,
    }, actor_did=party.get("did", ""))
    return case


# --------------------------------------------------------------------------
# Deterministic sweeps (liveness without a scheduler)
# --------------------------------------------------------------------------

def sweep(store) -> dict[str, Any]:
    """Apply every deterministic timeout rule. Idempotent; anyone may crank."""
    out = {"offers_expired": 0, "tasks_auto_settled": 0,
           "tasks_refunded_undelivered": 0, "cases_resolved": 0}
    now = _now()
    for offer in list((store.__dict__.get("offers") or {}).values()):
        deadline = datetime.fromisoformat(offer["core"]["deadline_at"])
        if offer["status"] == "open" and now >= deadline:
            with store.lock, store._txn():
                offer["status"] = "expired"
                if store.backend is not None:
                    store._persist_kv("offers", store.offers)
            if offer["escrow_id"]:
                esc = store.escrows.get(offer["escrow_id"])
                if esc and esc.get("status") == "funded":
                    try:
                        store.refund_escrow(offer["escrow_id"], esc["requester_key"])
                    except ValueError:
                        pass
            out["offers_expired"] += 1
        elif offer["status"] == "accepted" and offer.get("task_id"):
            task = store.tasks.get(offer["task_id"])
            esc = store.escrows.get(offer["escrow_id"]) if offer["escrow_id"] else None
            if not task or not esc or esc.get("status") != "funded":
                continue
            delivered = bool(task.get("deliverable_hash")
                             and (task.get("metadata") or {}).get("receipt_auth")
                             in ("worker_key", "worker_signature"))
            grace_end = deadline + timedelta(seconds=_grace_s())
            if delivered and now >= grace_end:
                # payer silence after authenticated delivery -> auto-settle
                try:
                    store.release_escrow(offer["escrow_id"], esc["requester_key"],
                                         deliverable_hash=task["deliverable_hash"],
                                         rating=0.75)
                    out["tasks_auto_settled"] += 1
                except ValueError:
                    pass
            elif not delivered and now >= grace_end:
                try:
                    store.refund_escrow(offer["escrow_id"], esc["requester_key"])
                    out["tasks_refunded_undelivered"] += 1
                except ValueError:
                    pass
    for case_id in list((store.__dict__.get("dispute_cases") or {}).keys()):
        before = store.dispute_cases[case_id]["status"]
        maybe_resolve(store, case_id)
        if before == "open" and store.dispute_cases[case_id]["status"] == "resolved":
            out["cases_resolved"] += 1
    return out
