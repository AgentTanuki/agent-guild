"""Deterministic, hash-bound objective matching for machine first contact.

No model participates in this trust-plane boundary. The matcher uses a
versioned alias table and exact token rules, returns offsets into the caller's
own UTF-8 bytes, and never returns caller-controlled text.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable
from urllib.parse import quote

from .demand import canonical_capability


CONTRACT = "AGCM-1/1.0"
ALIAS_VERSION = "2026-08-31"

# (phrase, canonical capability, specificity). Specificity prevents a generic
# phrase such as "review code" from competing with the more precise
# "security issues" in the same request. Equal-specificity distinct matches
# remain ambiguous; no statistical confidence score is invented.
ALIASES: tuple[tuple[str, str, int], ...] = (
    ("fact-checking", "fact-check", 2),
    ("fact checking", "fact-check", 2),
    ("fact-check", "fact-check", 2),
    ("verify claims", "fact-check", 2),
    ("verify a claim", "fact-check", 2),
    ("verify claim", "fact-check", 2),
    ("security issues", "security-review", 3),
    ("security review", "security-review", 3),
    ("security audit", "security-review", 3),
    ("vulnerability review", "security-review", 3),
    ("reviewing code", "code-review", 1),
    ("review code", "code-review", 1),
    ("code review", "code-review", 2),
    ("web research", "web-research", 2),
    ("research online", "web-research", 2),
    ("data extraction", "data-extraction", 2),
    ("extract data", "data-extraction", 2),
    ("hydrology dataset", "hydrology-analysis", 3),
    ("hydrology data", "hydrology-analysis", 3),
    ("analyse hydrology", "hydrology-analysis", 3),
    ("analyze hydrology", "hydrology-analysis", 3),
    ("summarization", "summarize", 2),
    ("summarize", "summarize", 2),
    ("translation", "translation", 2),
)

_KNOWN_CANONICAL = frozenset(canonical for _, canonical, _ in ALIASES)
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
_CAPABILITY_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]{0,63}", re.I)
_EXPLICIT_RE = re.compile(
    r"^\s*(?:capability|check|hire|vet)\b\s*[:=]?\s*"
    r"([a-z0-9][a-z0-9_.\-]{0,63})\s*$", re.I)
_OBJECTIVE_RE = re.compile(
    r"(?:\b(?:can|could|would|will)\s+you\b|"
    r"\b(?:need|find|looking|seeking|recommend|want|help|assist|perform|"
    r"handle|complete|who\s+can|analyse|analyze|review|delegate|hire|vet|"
    r"require|please)\b)", re.I)


def request_binding(text: str) -> dict[str, Any]:
    raw = text.encode("utf-8")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "utf8_bytes": len(raw)}


def looks_like_objective(text: str) -> bool:
    return bool(_OBJECTIVE_RE.search(text))


def canonical_input(text: str) -> str | None:
    """Return a safe canonical capability id, or ``None`` for prose.

    This check happens before demand recording and payment quoting on every
    capability-bearing trust surface.  It deliberately accepts unknown ids:
    an explicit ``korean-legal`` ask is legitimate unmet demand.  It rejects
    free text so a sentence can never be slugified into the demand namespace
    or relayed into a payment challenge.
    """
    stripped = (text or "").strip()
    if not stripped or _CAPABILITY_ID_RE.fullmatch(stripped) is None:
        return None
    return canonical_capability(stripped)


def _byte_span(text: str, start: int, end: int) -> dict[str, int]:
    return {
        "utf8_start": len(text[:start].encode("utf-8")),
        "utf8_end": len(text[:end].encode("utf-8")),
    }


def _candidate(canonical: str, text: str, start: int, end: int) -> dict[str, Any]:
    return {"canonical_capability": canonical,
            "span": _byte_span(text, start, end)}


def match(text: str, live_capabilities: Iterable[str] = ()) -> dict[str, Any]:
    binding = request_binding(text)
    catalog = _KNOWN_CANONICAL | frozenset(
        canonical_capability(cap) for cap in live_capabilities if cap)

    explicit = _EXPLICIT_RE.match(text)
    if explicit is not None:
        cap = canonical_capability(explicit.group(1))
        return {"contract": CONTRACT, "alias_version": ALIAS_VERSION,
                "request": binding, "kind": "exact_canonical",
                **_candidate(cap, text, explicit.start(1), explicit.end(1))}

    stripped = text.strip()
    whole = canonical_capability(stripped)
    if stripped and whole in catalog and _CAPABILITY_ID_RE.fullmatch(stripped):
        start = len(text) - len(text.lstrip())
        return {"contract": CONTRACT, "alias_version": ALIAS_VERSION,
                "request": binding, "kind": "exact_canonical",
                **_candidate(whole, text, start, start + len(stripped))}

    alias_hits: list[tuple[int, str, int, int]] = []
    for phrase, canonical, specificity in ALIASES:
        pattern = re.compile(
            r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", re.I)
        for found in pattern.finditer(text):
            alias_hits.append(
                (specificity, canonical, found.start(), found.end()))
    if alias_hits:
        strongest = max(hit[0] for hit in alias_hits)
        selected = [hit for hit in alias_hits if hit[0] == strongest]
        by_capability: dict[str, tuple[int, int]] = {}
        for _, canonical, start, end in selected:
            previous = by_capability.get(canonical)
            if previous is None or (end - start) > (previous[1] - previous[0]):
                by_capability[canonical] = (start, end)
        candidates = [
            _candidate(canonical, text, *span)
            for canonical, span in sorted(by_capability.items())
        ]
        if len(candidates) == 1:
            return {"contract": CONTRACT, "alias_version": ALIAS_VERSION,
                    "request": binding, "kind": "versioned_alias",
                    **candidates[0]}
        return {"contract": CONTRACT, "alias_version": ALIAS_VERSION,
                "request": binding, "kind": "ambiguous",
                "candidates": candidates}

    # Exact deterministic token containment over the server-owned catalog.
    # There is no embedding, fuzzy score, language model, or confidence claim.
    tokens = [(m.group(0).lower(), m.start(), m.end())
              for m in _TOKEN_RE.finditer(text)]
    token_set = {token for token, _, _ in tokens}
    token_hits: list[tuple[int, str, int, int]] = []
    for canonical in catalog:
        wanted = [t for t in re.split(r"[^a-z0-9]+", canonical) if t]
        if not wanted or not set(wanted).issubset(token_set):
            continue
        positions = [(start, end) for token, start, end in tokens
                     if token in set(wanted)]
        token_hits.append((len(wanted), canonical,
                           min(p[0] for p in positions),
                           max(p[1] for p in positions)))
    if token_hits:
        strongest = max(hit[0] for hit in token_hits)
        candidates = [
            _candidate(canonical, text, start, end)
            for score, canonical, start, end in sorted(token_hits)
            if score == strongest
        ]
        if len(candidates) == 1:
            return {"contract": CONTRACT, "alias_version": ALIAS_VERSION,
                    "request": binding, "kind": "deterministic_tokens",
                    **candidates[0]}
        return {"contract": CONTRACT, "alias_version": ALIAS_VERSION,
                "request": binding, "kind": "ambiguous",
                "candidates": candidates}

    return {"contract": CONTRACT, "alias_version": ALIAS_VERSION,
            "request": binding, "kind": "no_match"}


def probe_capsule(text: str) -> dict[str, Any]:
    return {
        "schema": "AGFC-1/1.0",
        "kind": "probe_ack",
        "state": "ready",
        "request": request_binding(text),
        "authority": {"mode": "advisory", "grants": []},
        "available_actions": [
            {"id": "trust.check", "effect": "read",
             "requires_local_authorisation": False,
             "call": {"transport": "a2a", "send": "check: <capability>"}},
            {"id": "capabilities.list", "effect": "read",
             "requires_local_authorisation": False,
             "call": {"transport": "a2a", "send": "capabilities"}},
            {"id": "incident.report", "effect": "persistent_write",
             "requires_local_authorisation": True,
             "call": {"transport": "a2a", "skill": "guild.report"}},
            {"id": "identity.register", "effect": "persistent_write",
             "requires_local_authorisation": True,
             "call": {"method": "POST", "path": "/agents/register"}},
        ],
        "details": {"agent_card": "/.well-known/agent-card.json",
                    "documentation": "/for-agents"},
    }


def objective_capsule(
    matched: dict[str, Any],
    full_check: dict[str, Any] | None,
    *,
    price_credits: int | None = None,
    action_path: str | None = None,
    action_id: str = "trust.check.full",
) -> dict[str, Any]:
    canonical = matched["canonical_capability"]
    if full_check is None:
        result = {"status": "mapping_only", "trust_decision": "not_included"}
    else:
        decision = full_check.get("decision") or {}
        routing = full_check.get("routing") or {}
        result = {
            "status": full_check.get("status"),
            "routable": bool(routing.get("routable")),
            "provider_id": routing.get("provider_id"),
            "estimate": decision.get("estimate"),
            "confidence": decision.get("confidence"),
            "reachability": decision.get("reachability_status"),
        }
    action = {
        "id": action_id,
        "effect": ("metered_read" if price_credits is not None else "read"),
        "requires_local_authorisation": price_credits is not None,
        "call": {"method": "GET",
                 "path": (action_path or
                          "/check?capability=" + quote(canonical, safe=""))},
    }
    if price_credits is not None:
        action["price_credits"] = price_credits
    return {
        "schema": "AGFC-1/1.0",
        "kind": "objective_match",
        "request": matched["request"],
        "match": {k: matched[k] for k in (
            "contract", "alias_version", "kind", "canonical_capability", "span")},
        "result": result,
        "authority": {"mode": "advisory", "grants": [],
                      "policy_owner": "caller"},
        "available_actions": [action],
    }


def unresolved_capsule(
        matched: dict[str, Any], *, transport: str = "a2a") -> dict[str, Any]:
    match_block = {k: v for k, v in matched.items() if k != "request"}
    if match_block.get("kind") == "ambiguous":
        candidates = match_block.get("candidates") or []
        # Keep the default reply bounded even if the live capability catalog
        # contains many equal-strength deterministic matches. The caller gets
        # the total plus a stable prefix and can inspect the linked catalog.
        match_block["candidate_count"] = len(candidates)
        match_block["candidates"] = candidates[:4]
    action_call = ({"transport": "a2a", "send": "capabilities"}
                   if transport == "a2a"
                   else {"method": "GET", "path": "/capabilities"})
    return {
        "schema": "AGFC-1/1.0",
        "kind": ("objective_ambiguous" if matched["kind"] == "ambiguous"
                 else "objective_no_match"),
        "request": matched["request"],
        "match": match_block,
        "authority": {"mode": "advisory", "grants": []},
        "available_actions": [{
            "id": "capabilities.list", "effect": "read",
            "requires_local_authorisation": False,
            "call": action_call,
        }],
    }


def note_followthrough(store: Any, actor: str, capability: str, *,
                       ua: str, endpoint: str, transport: str) -> None:
    """Link an explicit canonical retry to a recent compact mapping.

    This remains a deliberately labelled heuristic: transports do not share a
    conversation identifier.  It stores only the parent's request hash and
    canonical capability, never text, and counts a parent at most once.
    """
    parent = None
    for event in reversed(store.events[-500:]):
        if (event.get("key") == actor
                and event.get("type") == "query"
                and event.get("caller_kind") == "objective_ask"
                and event.get("capability") == capability):
            parent = event.get("request_sha256")
            break
    if not parent:
        return
    if any(event.get("type") == "objective_action_followed"
           and event.get("parent_request_sha256") == parent
           and event.get("action") == "trust.check.full"
           for event in store.events[-500:]):
        return
    store.record_event(
        actor, "objective_action_followed", ua=ua,
        endpoint=endpoint, transport=transport, capability=capability,
        action="trust.check.full", parent_request_sha256=parent,
        attribution="same_actor_capability_recent_tail")
