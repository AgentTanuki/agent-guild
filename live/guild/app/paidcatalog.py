"""ONE canonical description of what Agent Guild sells, rendered onto every
machine-readable discovery surface.

Why this module exists (2026-08-01)
-----------------------------------
The paid layer was live, correctly priced, fail-closed and x402-conformant —
and completely undiscoverable. Verified that day:

  * x402 Bazaar: 0 catalog entries for this host and 0 for each of the three
    operation names (CDP `/discovery/search`, queried directly).
  * The ONLY documented route into that catalog is a SETTLED payment through
    the CDP facilitator whose payload echoes the `bazaar` extension. There is
    no registration, publication or submission API — confirmed by omission
    across the x402 v2 spec, the bazaar extension spec and CDP's facilitator
    API reference, which exposes exactly two write endpoints (verify, settle).
  * That makes Bazaar listing UNREACHABLE while revenue is $0: it requires a
    paying counterparty, which is the very thing discovery is supposed to
    produce. Catalog presence is downstream of a first sale, not upstream.

So the next-strongest AUTOMATED machine-to-machine discovery surfaces are the
ones we already serve and that machines already read: the A2A agent card, MCP
tool descriptions, the well-known manifest, llms.txt and registry metadata.
They were all silent about the paid layer. An agent could not learn that a
deep preflight exists, what it costs, or what it returns, without first
tripping a 402 on a route it had no reason to call.

Design rules
------------
1. ONE source of truth. Prices come from `pricing`, never re-typed, so a
   surface can never advertise a price the gateway will not honour.
2. Every surface names the FREE alternative next to the paid one. An agent that
   should not pay must be able to see that from the offer itself.
3. Every surface has a STABLE SOURCE ID (`SOURCE_IDS`). Impressions are
   recorded per (operation, source) so "which surface produces qualified paid
   attention" is a measured question, not an opinion.
4. Nothing here invents an x402 field. The 402 challenge is built by
   `payments`/`x402` against the official v2 schema; this module is our OWN
   documentation surface and is deliberately separate from it.
"""
from __future__ import annotations

from typing import Any, Optional

from . import pricing

#: Stable identifiers for each machine-readable surface that can carry a paid
#: offer. These are recorded on every impression and MUST NOT be renamed —
#: renaming one silently splits its history in two.
SOURCE_IDS = (
    "paid_offer:agent_card",      # A2A agent card (/.well-known/agent-card.json)
    "paid_offer:mcp_tool",        # MCP tool description / discovery
    "paid_offer:manifest",        # /.well-known/agent-guild.json
    "paid_offer:llms_txt",        # /llms.txt
    "paid_offer:registry",        # MCP Registry publisher metadata
    "paid_offer:x402_challenge",  # the 402 itself (a caller already on a route)
)

#: The canonical paid operations, derived from the ACTUAL request
#: builders in `payments` and the ACTUAL public routes in `main`.
#:
#: ENTRYPOINT vs SETTLEMENT RESOURCE — these are NOT the same thing and this
#: module must never conflate them (Codex review, 2026-08-01, which caught an
#: earlier revision of this file advertising three call signatures that a buyer
#: could not execute):
#:
#:   * `entrypoint`  — what a machine actually CALLS. Method, path and where
#:                     the parameters go (query string vs JSON body). This is
#:                     an instruction, so it must be executable verbatim.
#:   * `settlement`  — the CANONICAL RESOURCE the x402 challenge binds and the
#:                     payment settles against, built by `payments.*_request`.
#:                     It is a canonicalised URL (sorted query, defaults
#:                     applied) and for a body-parameterised route it does NOT
#:                     look like the call you make. It is here so a third party
#:                     can match a catalog entry to a challenge — not so anyone
#:                     tries to call it.
#:
#: When they differ, both are published and labelled. When an operation has no
#: directly callable HTTP route at all (watch_cycle), we say exactly that and
#: point the machine at the flow that does exist.
_OPERATIONS: tuple[dict[str, Any], ...] = (
    {
        "operation": "machine_envelope",
        "entrypoint": {
            "protocol": "http",
            "method": "POST",
            "path": "/envelopes/issue",
            "query_params": None,
            "body": {
                "kind": "intent",
                "recipient": "did:key:<recipient or other identifier>",
                "payload_sha256": "<64 hex chars: digest of exact message bytes>",
                "nonce": "<caller-unique message id, 8..128 chars>",
                "ttl_seconds": 3600,
            },
            "auth": (
                "no account or API key required — use the same Base EOA for "
                "agent-guild/caller-proof-evm/v1 and x402 payment, or present "
                "the legacy did:key caller-proof/v1"),
            "key_required": False,
            "caller_proof_required": True,
            "directly_callable": True,
            "client": {
                "language": "javascript/typescript (node)",
                "source": "/sdk/agentguild_envelope_client.mjs",
                "factory": (
                    "createEvmMachineEnvelopeClient({evmSigner})"),
                "operation": (
                    "client.issue({payload, kind, recipient, nonce, ...})"),
                "dependencies": ["@x402/fetch", "@x402/evm"],
                "identity": (
                    "The same caller-owned Base EOA signs the exact request "
                    "with EIP-191 and pays x402; no registration or second "
                    "identity key is required."),
                "custody": (
                    "Payload bytes and signer private keys remain under "
                    "caller control and are never uploaded or persisted."),
            },
            # The exact settlement binding is an opaque server-derived hash of
            # normalized body + authenticated sender DID. It is deliberately
            # not a caller-supplied body field.
            "server_derived_settlement_params": ["request_sha256"],
        },
        "alternatives": {
            "mcp": ("guild_envelope_issue(kind=..., recipient=..., "
                    "payload_sha256=..., nonce=...)"),
            "verify": "guild_envelope_verify(envelope=<issued envelope>)",
        },
        "what_you_get": (
            "A privacy-preserving, Guild-signed machine communication envelope "
            "binding an authenticated sender DID to an exact payload "
            "digest, recipient, purpose, nonce, expiry and optional economic "
            "terms. The payload itself never reaches the Guild."),
        "why_it_is_worth_it": (
            "It turns ephemeral machine intent into a portable, non-repudiable "
            "record a recipient, arbiter or downstream agent can verify without "
            "trusting either participant. The scope is explicit: integrity and "
            "provenance, never an endorsement that the message is true."),
        "free_alternative": (
            "Create either supported Agent Guild caller proof yourself and "
            "send it directly with the message — free and sender-verifiable, "
            "but without the independent Guild issuance timestamp. Verifying "
            "any Guild envelope at POST /envelopes/verify is always free."),
    },
    {
        "operation": "payment_decision",
        "entrypoint": {
            "protocol": "http",
            "method": "POST",
            "path": "/wallet-binding/decision",
            "query_params": None,
            "body": {
                "payment": {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "asset": "<EVM token contract>",
                    "amount": "<positive atomic-unit integer string>",
                    "pay_to": "<exact counterparty EVM address>",
                    "resource": "<exact http(s) resource URL>",
                },
                "capability": "<optional required capability>",
                "policy": {"max_risk": 32.99, "min_confidence": 0.5},
                "ttl_seconds": 300,
            },
            "auth": "none required — pay per call from the 402 challenge, "
                    "or send X-API-Key to draw on a credit balance",
            "key_required": False,
            "directly_callable": True,
            "client": {
                "language": "javascript/typescript (node)",
                "source": "/sdk/integrations/x402_payment_policy.mjs",
                "factory": "createAgentGuildX402PaymentPolicy({meteredFetch})",
                "operation": "client.onBeforePaymentCreation(policy)",
                "dependencies": ["official x402 client"],
                "recursion_safety": (
                    "meteredFetch must be a separate unguarded x402 client; "
                    "a funded Agent Guild API key may use ordinary fetch"),
            },
            "server_derived_settlement_params": ["request_sha256"],
            "marketplace_transport": {
                "method": "POST",
                "body": {"request": "<exact payment decision request>",
                         "caller_proof": "<proof over RFC 8785 JCS(request)>"},
                "client": "/sdk/agentguild_envelope_client.mjs",
                "helper": "paymentDecisionMarketplaceInput",
                "discovery_guarantee": (
                    "an empty anonymous probe returns a non-executable quote; "
                    "an unsigned payment retry is rejected before settlement"),
                "relay_binding": (
                    "request.x402_resource_url must be the exact canonical "
                    "Payan buy URL and is sealed by caller proof"),
            },
        },
        "alternatives": {
            "free_identity_only": (
                "GET /wallet-binding/resolve?address=<payee>&network=<CAIP-2>"),
            "verify": "POST /wallet-binding/decision/verify",
        },
        "what_you_get": (
            "A short-lived W3C Verifiable Credential whose eddsa-jcs-2022 "
            "proof binds the exact selected payee, network, asset, atomic "
            "amount and resource to active wallet identity, current risk "
            "evidence, explicit thresholds and an allow/block decision."),
        "why_it_is_worth_it": (
            "It runs at the last reversible moment before a wallet signs. The "
            "buyer keeps a portable record of exactly why that irreversible "
            "payment was allowed or blocked, even if the Guild later goes "
            "offline."),
        "free_alternative": (
            "GET /wallet-binding/resolve is free and returns signed exact-"
            "wallet identity evidence, but no current risk evaluation or "
            "signed transaction-specific decision."),
    },
    {
        "operation": "deep_preflight",
        "entrypoint": {
            "protocol": "http",
            "method": "GET",
            "path": "/preflight/deep",
            "query_params": {"url": "<endpoint you are about to trust>"},
            "body": None,
            "auth": "none required — pay per call from the 402 challenge, "
                    "or send X-API-Key to draw on a credit balance",
            # STRUCTURED, so nothing downstream has to parse the prose. The
            # block-level auth statement is derived from these flags, which is
            # what stops it contradicting an operation again.
            "key_required": False,
            "directly_callable": True,
        },
        "alternatives": {
            "mcp": "guild_preflight_deep(url=\"<endpoint>\")",
            "a2a": "send 'preflight: <url>' (the FREE tier; the deep check is "
                   "HTTP/MCP only)",
        },
        "what_you_get": (
            "Live outbound verification of an endpoint before you trust or pay "
            "it, PLUS what one request cannot establish: drift history, "
            "cross-source corroboration, and an explicit allow / caution / "
            "block verdict."),
        "why_it_is_worth_it": (
            "Priced far below the mistake it prevents: an irreversible x402 "
            "transfer to an endpoint that does not answer."),
        "free_alternative": (
            "GET /preflight?url=<endpoint> — the full live check set and "
            "verdict, free and unauthenticated. It is NOT degraded to make the "
            "paid tier attractive. (MCP: guild_preflight; A2A: "
            "'preflight: <url>')"),
    },
    {
        "operation": "evidence_bundle",
        "entrypoint": {
            "protocol": "http",
            "method": "POST",
            "path": "/evidence/bundle",
            "query_params": None,
            # PARAMETERS GO IN THE JSON BODY. The settlement resource below
            # canonicalises them into a query string; that string is a binding
            # identifier, not a call you can make.
            "body": {"url": "<endpoint>", "ttl_seconds": 3600},
            "auth": "none required — pay per call from the 402 challenge, "
                    "or send X-API-Key to draw on a credit balance",
            "key_required": False,
            "directly_callable": True,
        },
        "alternatives": {},
        "what_you_get": (
            "A signed, portable, offline-verifiable evidence snapshot you "
            "keep, anchored to the published checkpoint feed with an inclusion "
            "proof. Re-verifiable without calling us again."),
        "why_it_is_worth_it": (
            "It survives us. A verdict you can only re-obtain by asking the "
            "issuer is not evidence, it is a subscription. Fails closed: if "
            "the bundle cannot be fully produced, signed and anchored, you are "
            "not charged."),
        "free_alternative": (
            "GET /agents/{id}/passport — the free Guild-signed passport, also "
            "offline-verifiable, without the bundled proof set."),
    },
    {
        "operation": "watch_cycle",
        "entrypoint": {
            "protocol": "http",
            "method": "POST",
            "path": "/watch",
            "query_params": None,
            "body": {"url": "<endpoint>", "interval_s": 3600},
            "auth": "X-API-Key REQUIRED — a watch bills per cycle, so it needs "
                    "an account to bill. POST /billing/trial issues credits "
                    "with no human involved.",
            "key_required": True,
            # THE HONEST BIT. `watch_cycle` is what you are billed for, and it
            # has NO directly callable public HTTP route: cycles are executed
            # by the Guild on your behalf, on the schedule you provisioned.
            # What you CALL is the free provisioning route above.
            "directly_callable": False,
            "callable_note": (
                "`watch_cycle` itself is NOT directly callable. It is the "
                "billing unit for a recheck the Guild performs on your behalf. "
                "You call the FREE provisioning entrypoint above; cycles then "
                "run on your interval and you are charged only for rechecks "
                "that actually happen. Read results with GET /watch/{watch_id} "
                "— free, you already paid for the cycles that produced it."),
        },
        "alternatives": {
            "mcp": "guild_watch(url=\"<endpoint>\", api_key=\"<key>\", "
                   "interval_seconds=3600) — provisioning; "
                   "guild_watch_feed to read it",
        },
        "what_you_get": (
            "Continuous re-verification of an endpoint you depend on, charged "
            "per recheck ACTUALLY performed."),
        "why_it_is_worth_it": (
            "A dormant endpoint costs you nothing, and provisioning is free — "
            "charging before any observation exists would be charging for a "
            "promise."),
        "free_alternative": (
            "Provisioning is free and idempotent by (caller, endpoint), and "
            "GET /watch/{watch_id} is free to read. Only executed rechecks "
            "bill. For a one-off answer instead, GET /preflight?url=<endpoint> "
            "is free."),
    },
)

#: Maps each operation to the `payments` builder that defines its canonical
#: settlement resource. The catalog DERIVES the resource from these rather than
#: restating it, so a builder change can never silently leave the catalog
#: advertising a stale binding — `tests/test_paid_catalog_binding.py` asserts
#: exactly that.
def _settlement_request(operation: str):
    from . import payments
    if operation == "deep_preflight":
        return payments.deep_preflight_request("<url>")
    if operation == "evidence_bundle":
        return payments.evidence_bundle_request("<url>")
    if operation == "watch_cycle":
        return payments.watch_cycle_request("<endpoint>")
    if operation == "machine_envelope":
        return payments.machine_envelope_request("<request_sha256>")
    if operation == "payment_decision":
        return payments.payment_decision_request("<request_sha256>")
    raise ValueError(f"no settlement binding for {operation!r}")


def _base_url(base: Optional[str] = None) -> str:
    import os
    return (base or os.environ.get("GUILD_PUBLIC_URL")
            or "https://agent-guild-5d5r.onrender.com").rstrip("/")


def price_usd(operation: str) -> str:
    """Current effective price as a USD string. Reads `pricing` live so an
    experiment-applied override can never disagree with what we advertise."""
    credits = pricing.price(operation)
    return f"${credits / 1000:.3f}".rstrip("0").rstrip(".") or "$0"


def operations(base: Optional[str] = None) -> list[dict[str, Any]]:
    """The catalog, priced live and BOUND LIVE.

    Both the price and the canonical settlement resource are read from the code
    that actually charges (`pricing`, `payments.*_request`), never restated
    here, so a surface can never advertise a price the gateway will not honour
    or a resource the challenge will not bind."""
    root = _base_url(base)
    out = []
    for op in _OPERATIONS:
        name = op["operation"]
        preq = _settlement_request(name)
        ep = dict(op["entrypoint"])
        # The executable instruction, rendered from the REAL route.
        call = f"{ep['method']} {root}{ep['path']}"
        if ep.get("query_params"):
            call += "?" + "&".join(f"{k}={v}" for k, v in
                                   ep["query_params"].items())
        ep["call"] = call
        if ep.get("body"):
            ep["body_example"] = ep["body"]
        # Static catalog definitions use root-relative artifact paths so they
        # remain deployment-neutral. Every served discovery document receives
        # a directly fetchable absolute URL.
        if ep.get("client"):
            client = dict(ep["client"])
            source = client.get("source")
            if isinstance(source, str) and source.startswith("/"):
                client["source"] = root + source
            ep["client"] = client
        out.append({
            "operation": name,
            "price_usd": price_usd(name),
            "price_credits": pricing.price(name),
            "payment": "x402 (USDC on Base mainnet, eip155:8453)",
            # HOW YOU CALL IT — executable verbatim.
            "entrypoint": ep,
            # WHAT THE PAYMENT BINDS — an identifier, not a call. Derived from
            # payments.*_request so it cannot drift from the challenge.
            "settlement": {
                "method": preq.method,
                "canonical_resource": preq.resource_url,
                "note": ("The canonical resource the x402 challenge binds and "
                         "the payment settles against. Parameters are "
                         "canonicalised into the query string even when the "
                         "call itself sends them in a JSON body — match a "
                         "challenge to this, do not try to call it."),
                "differs_from_entrypoint": (
                    preq.method != ep["method"]
                    or bool(ep.get("body"))
                    or not ep.get("directly_callable", True)),
            },
            "directly_callable": ep.get("directly_callable", True),
            "callable_note": ep.get("callable_note"),
            "alternatives": op.get("alternatives") or {},
            "what_you_get": op["what_you_get"],
            "why_it_is_worth_it": op["why_it_is_worth_it"],
            "free_alternative": op["free_alternative"],
        })
    return out


def _authentication_block(base: Optional[str] = None) -> dict[str, Any]:
    """The block-level auth statement, DERIVED from each operation's own
    `key_required` flag.

    It used to be a hand-written slogan — "No account, no subscription, no
    human in the loop" — which flatly contradicted watch_cycle's own entrypoint
    (X-API-Key REQUIRED). A machine that believed the top-level line would call
    POST /watch and receive a 401 it had been told could not happen. Deriving
    it means the two cannot disagree again."""
    ops = operations(base)
    keyless = [o["operation"] for o in ops
               if not o["entrypoint"].get("key_required")]
    keyed = [o["operation"] for o in ops
             if o["entrypoint"].get("key_required")]
    out: dict[str, Any] = {
        "keyless_operations": keyless,
        "key_required_operations": keyed,
        "never": "No subscription, and no human in the loop either way.",
    }
    if keyless:
        out["one_off_operations"] = (
            f"{' and '.join(keyless)} need NO account: pay per call from the "
            "402 challenge.")
    if keyed:
        out["watches"] = (
            f"{' and '.join(keyed)} bill per unit of work actually performed, "
            "so they need an account to bill: the provisioning call requires "
            "X-API-Key. Provisioning itself is free, and the key is self-serve "
            "— POST /billing/trial issues credits with no human involved.")
    return out


def offer_block(source: str, base: Optional[str] = None) -> dict[str, Any]:
    """The paid-offer block for one surface, carrying that surface's stable
    source id so a follow-through can be attributed to where it was seen."""
    if source not in SOURCE_IDS:
        raise ValueError(f"unknown paid-offer source id {source!r}")
    return {
        "source": source,
        "operations": operations(base),
        "honesty": (
            "Every paid operation above has a free alternative named beside "
            "it. If the free one answers your question, use it — we would "
            "rather be the default than be paid once."),
        # EXACT, not slogan-shaped. The blanket "no account" claim
        # contradicted watch_cycle's own entrypoint, which says X-API-Key
        # REQUIRED — and a machine that believed the top-level line would call
        # POST /watch and get a 401 it was told could not happen. A discovery
        # surface that is wrong about its own auth is worse than one that says
        # nothing.
        "authentication": _authentication_block(base),
    }


def llms_txt_section(base: Optional[str] = None) -> str:
    """Plain-text rendering for /llms.txt. Prints the CALLABLE entrypoint, not
    the settlement identifier — a machine reading this should be able to copy
    the line and have it work."""
    lines = ["## Paid operations (x402, USDC on Base mainnet)",
             "Each paid call names its free alternative. Use the free one if "
             "it answers your question.", ""]
    for op in operations(base):
        lines.append(f"- {op['operation']} — {op['price_usd']} per call")
        if op["directly_callable"]:
            lines.append(f"  call:     {op['entrypoint']['call']}")
            if op["entrypoint"].get("body_example"):
                import json as _json
                lines.append("  body:     "
                             + _json.dumps(op["entrypoint"]["body_example"]))
        else:
            lines.append(f"  NOT directly callable. {op['callable_note']}")
            lines.append(f"  provision: {op['entrypoint']['call']}")
            if op["entrypoint"].get("body_example"):
                import json as _json
                lines.append("  body:      "
                             + _json.dumps(op["entrypoint"]["body_example"]))
        lines.append(f"  auth:     {op['entrypoint']['auth']}")
        lines.append(f"  settles:  {op['settlement']['method']} "
                     f"{op['settlement']['canonical_resource']}"
                     + ("  (binding identifier, not a call)"
                        if op["settlement"]["differs_from_entrypoint"] else ""))
        lines.append(f"  you get:  {op['what_you_get']}")
        lines.append(f"  free instead: {op['free_alternative']}")
        lines.append("")
    lines.append("Source id for attribution: paid_offer:llms_txt")
    return "\n".join(lines)


def mcp_discovery_meta(base: Optional[str] = None) -> dict[str, Any]:
    """Namespaced `_meta` for the paid-catalog MCP tool's `tools/list` entry.

    WHY THIS EXISTS. An external MCP crawler reads `tools/list` and nothing
    else before deciding what a server offers. On 2026-08-01 an independent
    reliability probe read ours and reported
    `payment.access=unknown, payment.x402=false, "no pricing or payment
    metadata advertised"` — correctly, because the entry carried a prose
    description, an empty `inputSchema`, an opaque `outputSchema` and
    `_meta: {"fastmcp": {"tags": []}}`. Everything a buyer needs was one call
    away and nothing said so in a form a parser could read.

    DELIBERATELY CARRIES NO PRICE. `_meta` is fixed when the tool is
    registered, so a number here would freeze at boot and drift the moment the
    experiment engine moved a price within its ceiling. A stale price is worse
    than an absent one. Operation NAMES are derived from `_OPERATIONS` (never
    retyped, so a new operation appears automatically) and the current price
    lives behind the pointers below — one free, unauthenticated call.

    NOT A CLAIMED STANDARD. There is no MCP payment annotation to conform to,
    so this uses the spec's own extension point (`_meta`) under our own
    namespace rather than inventing a shape and implying it is standard. The
    behavioural hints that ARE standard are set separately via
    `ToolAnnotations`."""
    root = _base_url(base)
    return {
        "ai.agent-guild/paid": {
            "payment_protocol": "x402",
            "network": "eip155:8453",
            "asset": "USDC",
            "autonomous": True,
            "human_in_the_loop": False,
            "account_required": False,
            "operations": [op["operation"] for op in _OPERATIONS],
            "free_alternative_exists_for_every_operation": True,
            "price_source": {
                "note": ("Prices are LIVE and are not published here — this "
                         "block is static per build and would go stale. Call "
                         "the tool (free, no account) or GET the catalog URL."),
                "mcp_tool": "guild_paid_operations",
                "http_catalog": (f"{root}/.well-known/agent-guild.json"
                                 "?src=paid_offer:registry"),
            },
            "returns": ("per operation: current price, exact callable "
                        "entrypoint, canonical x402 settlement resource, and "
                        "the free alternative"),
        }
    }


def mcp_discovery_output_schema() -> dict[str, Any]:
    """`outputSchema` for the paid-catalog tool.

    It was `{"type": "object", "additionalProperties": true}` — technically
    true and completely opaque, so a crawler could not learn the response shape
    without calling. Declaring the shape is the cheapest machine legibility
    available. Kept PERMISSIVE (`additionalProperties: true`, minimal
    `required`) so it documents without constraining: an over-tight schema on a
    real tool breaks calls, which would be a far worse outcome than vagueness."""
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "source": {"type": "string"},
            "authentication": {"type": "object"},
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "operation": {"type": "string"},
                        "price_usd": {"type": "string",
                                      "description": "current, live"},
                        "price_credits": {"type": "integer"},
                        "payment": {"type": "string"},
                        "entrypoint": {
                            "type": "object", "additionalProperties": True,
                            "description": ("how to CALL it: method, path, "
                                            "where parameters go, auth"),
                        },
                        "settlement": {
                            "type": "object", "additionalProperties": True,
                            "description": ("canonical resource the x402 "
                                            "challenge binds; an identifier, "
                                            "not a call"),
                        },
                        "what_you_get": {"type": "string"},
                        "free_alternative": {"type": "string"},
                        "directly_callable": {"type": "boolean"},
                    },
                },
            },
        },
    }
