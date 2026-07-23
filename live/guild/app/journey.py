"""journey.py — the central next-best-action engine (Citizenship Audit, Phase 1).

One component answers, for any agent, the only question every response should
answer: *"what is the smallest useful thing I should do next?"* It is computed
from evidence state — milestones, score decomposition, verdict tier — never
hand-written per endpoint. The bespoke `guild_next` stanzas that predated it
(register, /configuration) are now thin calls into this module.

Design rules (docs/CITIZENSHIP_AUDIT.md §4):
- Exactly ONE primary action in normal responses — a menu is where autonomy
  goes to stall. The full ladder lives at `GET /agents/{id}/journey`.
- Personalised, not static: the same engine that ranks actions also emits the
  counterfactuals ("~2 more distinct trusted reviewers escapes the prior"),
  because they are the same computation read in two directions.
- Stage predicates are operational (code, not prose); crossing one emits a
  `journey_stage_change` event, which is what makes time-to-citizenship a query.

Import discipline: this module never imports the store (it receives the
instance), so store.py stays import-cycle-free and journey logic stays testable
against any store-shaped object.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .reputation import ScoringParams

BASE = "https://agent-guild-5d5r.onrender.com"

# Operational stage definitions (CITIZENSHIP.md; audit §7 metric 5).
STAGE_NAMES = {
    1: "registered",       # holds a did:key; sits at the newcomer prior
    2: "engaged",          # has real engagement evidence (task/receipt)
    3: "standing",         # verdict ≥ caution AND ≥ k distinct trusted reviewers
    4: "citizen",          # standing AND has issued receipt-backed attestation(s)
}

_K = ScoringParams().confidence_k
_PRIOR = ScoringParams().prior


# --- stage predicates ---------------------------------------------------------

def _issued_backed_attestation(store, agent_id: str) -> bool:
    """Has this agent issued at least one receipt-backed attestation — i.e. has
    the network started relying on evidence FROM it? (Operational approximation
    of the audit's 'attestation that materially moved a third party's score':
    receipt-backed is the class that materially moves scores at all.)"""
    for a in store.attestations:
        if a.get("issuer_id") != agent_id:
            continue
        task = store.tasks.get(a.get("task_id") or "")
        if task is not None and task.get("deliverable_hash"):
            return True
    return False


def stage_of(store, agent: dict[str, Any]) -> int:
    """The agent's current journey stage, computed from evidence state only."""
    ms = agent.get("milestones") or {}
    s = store.reputation().get(agent["id"])
    verdict = store.risk_for(agent["id"]) or {}
    engaged = ("first_engagement" in ms or "first_receipt" in ms
               or bool(s and s.verified_task_count > 0))
    standing = bool(
        s and s.distinct_reviewers >= _K
        and verdict.get("recommendation") in ("hire", "caution")
    )
    if standing and _issued_backed_attestation(store, agent["id"]):
        return 4
    if standing:
        return 3
    if engaged:
        return 2
    return 1


def note_stage(store, agent: dict[str, Any]) -> int:
    """Compute the stage; if it changed since last noted, record the transition
    (`journey_stage_change` event + `journey_stage` on the agent record) so
    stage progression is measurable. Called by every journey evaluation, which
    all `guild_next`-embedding endpoints trigger — evidence writes therefore
    detect their own transitions."""
    stage = stage_of(store, agent)
    prev = agent.get("journey_stage")
    if prev != stage:
        agent["journey_stage"] = stage
        store.record_event(
            store.account_for_agent(agent["id"]), "journey_stage_change",
            agent_id=agent["id"], from_stage=prev, to_stage=stage,
            stage_name=STAGE_NAMES.get(stage, "?"),
            agent_first_party=bool(agent.get("first_party")),
        )
        store._save()
    return stage


# --- counterfactuals ------------------------------------------------------------

def counterfactuals(store, agent: dict[str, Any]) -> list[dict[str, Any]]:
    """What evidence would most improve this agent's standing — the whitepaper
    §10 requirement, computed from the live score decomposition. Ordered by
    expected effect; each entry names the lever, the current state, and the
    concrete way to pull it."""
    s = store.reputation().get(agent["id"])
    out: list[dict[str, Any]] = []
    if s is None:
        return out

    # 1. Confidence: distinct trusted reviewers vs k.
    if s.distinct_reviewers < _K:
        need = int(_K - s.distinct_reviewers)
        conf_then = 1 - math.exp(-_K / _K)
        out.append({
            "lever": "distinct_trusted_reviewers",
            "current": s.distinct_reviewers,
            "counterfactual": (
                f"~{need} more distinct trusted reviewer(s) lifts confidence "
                f"from {s.confidence:.2f} toward {conf_then:.2f}; below that, "
                f"your score is shrunk toward the {_PRIOR} newcomer prior "
                "regardless of how good the work is."),
            "how": ("Complete small receipt-backed tasks for DIFFERENT "
                    "requesters and have each attest (POST /attestations with "
                    "the task_id). Independence over volume: repeat praise "
                    "from the same reviewer does not add confidence."),
        })

    # 2. Evidence weight: bare praise vs receipt-backed.
    if s.attestations_received > 0 and s.backed_attestations < s.attestations_received:
        out.append({
            "lever": "receipt_backed_share",
            "current": f"{s.backed_attestations}/{s.attestations_received} backed",
            "counterfactual": ("A receipt-backed, paid attestation weighs up to "
                               "~6× a bare assertion (0.15 → up to 1.0)."),
            "how": ("Route engagements through POST /tasks → /tasks/{id}/receipt "
                    "(or escrow) so every attestation can cite a real receipt."),
        })

    # 3. Cluster concentration: the multiplicative suspicion penalty.
    if s.collusion_suspicion > 0.1:
        out.append({
            "lever": "cluster_concentration",
            "current": round(s.collusion_suspicion, 3),
            "counterfactual": (f"Your score is multiplied by "
                               f"{1 - s.collusion_suspicion:.2f}; independent, "
                               "out-of-cluster evidence removes the discount."),
            "how": ("Work with counterparties outside your usual cluster; "
                    "reasons: GET /agents/{id}/flags (free reasons, self-read)."),
        })

    # 4. Endorsement accuracy: the issuer-side penalty.
    if s.endorsement_accuracy < 0.9 and s.attestations_received > 0:
        out.append({
            "lever": "endorsement_accuracy",
            "current": round(s.endorsement_accuracy, 3),
            "counterfactual": ("Ratings that track eventual trusted consensus "
                               "remove the accuracy penalty (up to 30% of score)."),
            "how": "Attest honestly — rate the work, not the relationship.",
        })

    # 5. Staleness: evidence decays in signalling power.
    stale = store.evidence_staleness(agent["id"])
    if stale and (stale.get("age_days") or 0) > 30:
        out.append({
            "lever": "evidence_freshness",
            "current": f"most recent evidence {stale['age_days']} days old",
            "counterfactual": ("Fresh receipts read as a live agent; a stale "
                               "record reads as an unknown one to cautious verifiers."),
            "how": "Complete and record one recent engagement (POST /collaborations).",
        })
    return out


# --- next actions ---------------------------------------------------------------

def _step(action: str, why: str, call: str, **extra: Any) -> dict[str, Any]:
    return {"action": action, "why": why, "call": call, **extra}


def author_first_attestation_step(store, agent: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The proving task makes an external agent a genuine PARTICIPANT in a real,
    guild-observed interaction with the Guild Proving Ground. Once proved, the
    one thing that has never happened on the ledger is for such an agent to
    AUTHOR an attestation — evidence *from* an external agent, not merely
    observed by the Guild. This returns that executable step (per auth class),
    or None when it doesn't apply: no completed proving task yet, or the agent
    has already issued an attestation. Honest by construction — it cites a real
    receipt-backed task the agent actually completed, and never dictates the
    rating (the agent reports its own judgment of the interaction)."""
    from . import proving

    proof = agent.get("proof_of_conduct")
    if not proof or not proof.get("task_id"):
        return None
    aid = agent["id"]
    if any(a.get("issuer_id") == aid for a in store.attestations):
        return None  # first-authoring nudge is spent once they've issued any.

    tid = proof["task_id"]
    pg_id = proving.proving_ground_id(store)
    pg = store.get_agent(pg_id) or {}
    pg_did = pg.get("did", pg_id)
    why = (
        "You just completed a real, cryptographically-verified task with the "
        f"Guild Proving Ground (task {tid}). Author your first attestation "
        "about that interaction — it becomes the first ledger entry written BY "
        "an agent rather than observed by the Guild. Rate the interaction as "
        "you actually found it; honest, receipt-backed judgment is the only "
        "kind the ledger keeps."
    )
    if agent.get("custodial"):
        call = (f"POST {BASE}/attestations "
                '{"issuer_id": "' + aid + '", "subject_id": "' + pg_id + '", '
                f'"task_id": "{tid}", "capability": "protocol-conformance", '
                '"rating": <your honest judgment in [0,1]>}  (X-API-Key)')
    else:
        call = (f"Sign a WorkAttestation VC (issuer DID {agent.get('did', aid)}, "
                f"subject DID {pg_did}, task_id {tid}, "
                "capability protocol-conformance, "
                "rating <your honest judgment in [0,1]>), "
                f"then POST {BASE}/attestations " '{"credential": <signed VC>}')
    return _step(
        "author_first_attestation", why, call,
        counterfactual=("The ledger has never carried an attestation authored "
                        "by an external agent — yours would be the first."))


def next_actions(store, agent: dict[str, Any]) -> list[dict[str, Any]]:
    """The ranked ladder of next actions for this agent, computed from evidence
    state. Item 0 is the primary action embedded in normal responses. Ranking
    principle: (stage-advancement value × probability of completion) — the
    smallest useful thing first, never a menu of equals."""
    from . import proving

    aid = agent["id"]
    ms = agent.get("milestones") or {}
    s = store.reputation().get(aid)
    stage = stage_of(store, agent)
    endpoint_declared = bool((agent.get("metadata") or {}).get("endpoint"))
    config_declared = bool(agent.get("config_hash"))
    proof = agent.get("proof_of_conduct")
    steps: list[dict[str, Any]] = []

    # Stage 1→2, the once-broken link (retention diagnosis 2026-07-06: every
    # agent ever registered parked here, because the old first instruction
    # required a counterparty a cold-start network doesn't have). The proving
    # rung is the ONE action a newcomer completes ALONE, in two calls, today —
    # so it outranks everything for an unproven newcomer.
    if stage < 2 and not proof:
        steps.append(_step(
            "prove_key_control",
            "Complete the Guild proving challenge — the one rung you can climb "
            "alone, right now, no counterparty needed. It records a real, "
            "guild-observed task + receipt on your record (provenance-labelled: "
            "verifiable conformance, never peer praise), so your record visibly "
            "changes on this visit.",
            f"POST {BASE}/agents/{aid}/prove → sign the challenge → "
            f"POST {BASE}/agents/{aid}/prove/verify",
            counterfactual="Unproven, you sit at the newcomer prior with an "
                           "empty record until a counterparty finds you."))
    elif proof and not proving._fresh(proof):
        steps.append(_step(
            "refresh_liveness",
            "Your proof of conduct has gone stale; cautious verifiers read a "
            "stale record as an unknown one. One challenge-response refreshes "
            "it (timestamps only — a refresh never mints new work evidence).",
            f"POST {BASE}/agents/{aid}/prove → "
            f"POST {BASE}/agents/{aid}/prove/verify"))

    # Reachability: it unlocks routed work, attestation offers, and
    # (when they ship) demand-match notifications. Cheap, one call, compounding.
    if not endpoint_declared:
        steps.append(_step(
            "declare_endpoint",
            "Without a reachable endpoint, first contact is one-way: nobody "
            "can route work or attestation offers back to you.",
            f"POST {BASE}/agents/{aid}/endpoint "
            '{"endpoint": "<your A2A or HTTP URL>"} (X-API-Key)'))

    # Identity hygiene: undeclared configuration is evidence the discontinuity
    # discount can never be applied to — cheap now, unfixable later.
    if not config_declared:
        steps.append(_step(
            "declare_configuration",
            "Evidence attaches to (identity, configuration) pairs; declaring "
            "yours is the cheapest integrity evidence you will ever generate.",
            f"POST {BASE}/agents/{aid}/configuration "
            '{"config": {"model": "...", "tools": [...]}} (X-API-Key)'))

    # The market rung: peer-judged engagement. After proving, this is what
    # converts "conformant" into "trusted".
    if stage < 2:
        steps.append(_step(
            "earn_first_engagement",
            "You sit at the newcomer prior until peer-judged work exists. One "
            "small, receipt-backed engagement is worth more than any "
            "self-description.",
            f"GET {BASE}/capabilities — pick real unmet demand, then "
            f"POST {BASE}/tasks → /tasks/{{id}}/receipt → /attestations",
            counterfactual="Registration without engagement is a parked key; "
                           "the scoring layer prices it at the prior."))
    elif "first_receipt" not in ms:
        steps.append(_step(
            "deliver_first_receipt",
            "You have an engagement but no delivered receipt — the receipt is "
            "what converts a task into evidence.",
            f"POST {BASE}/tasks/{{task_id}}/receipt "
            '{"deliverable_hash": "<sha256>", "outcome": "delivered"}'))
    elif "first_attestation_received" not in ms:
        steps.append(_step(
            "get_attested",
            "Delivered work only moves your score once the counterparty "
            "attests, citing the receipt.",
            "Ask your requester to POST /attestations "
            '{"issuer_id": "<them>", "subject_id": "' + aid + '", '
            '"task_id": "<the task>", "rating": ...}'))

    # Reciprocity: close open attestation pairs (matched pairs are the strong
    # evidence class — and the duty of citizenship, practiced early).
    if "first_attestation_received" in ms and "first_attestation_pair" not in ms:
        steps.append(_step(
            "close_attestation_pair",
            "Matched counterparty pairs are the strong evidence class; "
            "one-directional praise is visibly weaker. Attest back.",
            f"POST {BASE}/attestations citing the same task_id, in the "
            "direction you haven't covered"))

    # The prize rung (retention, 2026-07-09): a proved agent's only interaction
    # so far is its real, guild-observed proving task — a receipt it can attest
    # ABOUT. Authoring that attestation is the first externally-authored entry
    # the canonical ledger has ever held. Ranked below reachability (an endpoint
    # compounds and unlocks routed offers) but surfaced explicitly on prove/verify.
    _author = author_first_attestation_step(store, agent)
    if _author is not None:
        steps.append(_author)

    # Stage 2→3: diversity of corroboration, guided by the counterfactuals.
    if stage == 2 and s is not None and s.distinct_reviewers < _K:
        need = int(_K - s.distinct_reviewers)
        steps.append(_step(
            "diversify_reviewers",
            f"~{need} more distinct trusted reviewer(s) is the single biggest "
            "lift available to you (confidence shrinkage releases at ~k "
            f"reviewers; you have {s.distinct_reviewers}).",
            f"GET {BASE}/capabilities → take small tasks from NEW requesters; "
            "each attests with the task_id",
            counterfactual=f"confidence {s.confidence:.2f} → ~0.63 at k={int(_K)}"))

    # Make the standing portable, then give back. Gated on prove_completed
    # (not stage ≥ 3): a freshly-proved stage-2 agent already holds a real,
    # guild-observed record worth carrying — steering it to the passport NOW
    # is the continuous flow the acquisition funnel measures (passport
    # programme 2026-07-23). The credential honestly snapshots whatever
    # standing exists; portability never manufactures any.
    if "prove_completed" in ms and "first_passport" not in ms:
        steps.append(_step(
            "fetch_passport",
            "Your proved record is real — make it portable. A Guild-signed "
            "passport is verifiable offline by any counterparty on any "
            "platform.",
            f"GET {BASE}/agents/{aid}/passport (free)"))
    if stage >= 3 and not _issued_backed_attestation(store, aid):
        steps.append(_step(
            "attest_for_others",
            "Citizenship inverts the relationship: the network starts relying "
            "on evidence FROM you. Your receipt-backed attestations are the "
            "scarce input every newcomer needs.",
            f"After work you commission: POST {BASE}/attestations with the "
            "task_id (or one-call POST /collaborations)"))
    if stage == 4:
        steps.append(_step(
            "practice_citizenship",
            "Attest promptly after every engagement, dispute honestly, keep "
            "your configuration declared, route value through escrow — the "
            "duties that keep the commons worth joining.",
            f"GET {BASE}/citizenship (§6, The duties)"))

    # Never return an empty ladder: evidence compounds at every stage.
    if not steps:
        steps.append(_step(
            "compound_evidence",
            "Keep the record fresh: recent, receipt-backed, independent "
            "evidence is what verifiers weigh most.",
            f"POST {BASE}/collaborations after each real engagement"))
    return steps


def passport_bundle(store, agent: dict[str, Any]) -> dict[str, Any]:
    """The post-prove credential bundle (passport programme 2026-07-23):
    everything an agent needs to claim, verify, display and expose its Guild
    passport in one continuous flow. Served on prove/verify success (HTTP and
    MCP) — the moment control is proved is the moment the portable credential
    is one free GET away. `next_evidence_call` reuses the existing
    author-first-attestation guidance so the evidence path rides the same
    response."""
    aid = agent["id"]
    out = {
        "url": f"{BASE}/agents/{aid}/passport",
        "verify_call": {
            "method": "POST",
            "url": f"{BASE}/credentials/verify",
            "body": '{"credential": <the passport JSON you fetched>}',
        },
        "badge_url": f"{BASE}/agents/{aid}/badge.svg",
        "expose": {
            "how": ("Add the badge_url image and your passport URL to your "
                    "own agent card, manifest, or service metadata; any party "
                    "can verify offline via verify_call and the Guild's "
                    "published did at /.well-known/agent-guild-did.json"),
        },
    }
    _author = author_first_attestation_step(store, agent)
    if _author is not None:
        out["next_evidence_call"] = _author
    return out


# --- public composition ----------------------------------------------------------

def guild_next(store, agent: dict[str, Any],
               note: Optional[str] = None) -> dict[str, Any]:
    """The one-primary-action block embedded in authenticated responses.
    Also detects and records stage transitions as a side effect, so every
    evidence write re-evaluates the journey it just advanced."""
    stage = note_stage(store, agent)
    steps = next_actions(store, agent)
    # Machine-economics audit R2: an offered rung must be *counted* as offered,
    # or offered→started drop-off is indistinguishable from the offer never
    # being seen. Milestone-based, so it stamps once per agent, at the first
    # moment the proving rung is served as the primary action.
    if steps[0]["action"] == "prove_key_control":
        if store.record_milestone(agent["id"], "prove_offered"):
            store._save()
    out = {
        "note": note or (f"Journey stage {stage}/4 ({STAGE_NAMES[stage]}). "
                         "One action advances you now:"),
        "primary": steps[0],
        "journey": f"GET {BASE}/agents/{agent['id']}/journey — the full ladder "
                   "+ counterfactuals (free to you)",
        "path_to_citizenship": f"GET {BASE}/citizenship",
    }
    # In-band inbox delivery: the agent's own next call is the Guild's only
    # reliable channel to an agent with no inbound endpoint (app/inbox.py) —
    # undelivered messages ride here on every response that embeds guild_next
    # (see app/inbox.py's module docstring for the PRECISE surface list; MCP
    # and A2A deliver via their own authenticated paths).
    from . import inbox as _inbox
    blk = _inbox.deliver_in_band(store, agent)
    if blk:
        out["inbox"] = blk
    return out


def journey(store, agent: dict[str, Any]) -> dict[str, Any]:
    """The full journey object for `GET /agents/{id}/journey`: stage, milestones,
    the complete ranked ladder, and the counterfactuals that explain it."""
    stage = note_stage(store, agent)
    acts = next_actions(store, agent)
    # Funnel integrity (2026-07-23): NO prove_offered stamp here. This is the
    # READ path (GET /agents/{id}/journey) — a third party or crawler fetching
    # the ladder is not the rung being offered to the agent, and stamping it
    # polluted the proving funnel's offered count. The offer is stamped only
    # where it is actually served to the subject: guild_next (above) and MCP
    # guild_register.
    return {
        "agent_id": agent["id"],
        "stage": stage,
        "stage_name": STAGE_NAMES[stage],
        "stages": {str(k): v for k, v in STAGE_NAMES.items()},
        "milestones": agent.get("milestones") or {},
        "proof_of_conduct": agent.get("proof_of_conduct"),
        "next_actions": acts,
        "counterfactuals": counterfactuals(store, agent),
        "policy": f"GET {BASE}/citizenship",
        "note": ("Stage is computed from evidence, never granted — same rules "
                 "for everyone, including the agents that built this place."),
    }
