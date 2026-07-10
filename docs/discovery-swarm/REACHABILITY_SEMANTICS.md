# Reachability semantics

2026-07-10 · Enforced by `live/guild/app/reachability.py` (single source for every surface: `/check`, shortlists, A2A, MCP). Motivating evidence: `evidence/external-actor-4580505b.md` — an external agent polled `check: fact-check` ~29× because a recommendation carried no reachability truth.

Core principle: **an endpoint string is a claim, not a route.** A status may never imply a check that did not run. `DECLARED_UNVERIFIED` is never described as "reachable".

## Status ladder

| Status | Entered by | Expires | Verification method | External machine may safely infer | Guild may RECOMMEND? | Guild may ROUTE work? |
|---|---|---|---|---|---|---|
| `no_endpoint` | agent has never declared an endpoint | on declaration | none | there is no route; do not attempt contact via the Guild | only with the no-route disclosure block (`supply_has_no_declared_endpoint`) | NO |
| `declared_unverified` | owner declares a well-formed http(s) URL (`POST /agents/{id}/endpoint`, authenticated) | never (no verifier exists) | `declaration_only` | a URL exists that the AGENT claims is theirs; nobody has checked it; treat as untrusted input | YES, with disclosure — the status field itself is the disclosure | NO |
| `unknown` | endpoint string on file but malformed (non-http(s), unparseable) | on re-declaration | none | the record is broken; worse than absent | NO | NO |
| `recently_reachable` (reserved) | SSRF-safe declaration-time liveness check succeeded | 24 h → back to `declared_unverified` | `declaration_probe` | the URL answered HTTP recently; says nothing about invocation semantics | YES | YES, marked best-effort |
| `currently_unreachable` (reserved) | the last verification attempt failed | 24 h → `declared_unverified` | `declaration_probe` | the URL did NOT answer recently; prefer another supplier | only with failure disclosure | NO |
| `invocation_verified` (reserved) | a guild-observed task receipt travelled through this endpoint | 7 d → `recently_reachable` | `guild_observed_receipt` | the endpoint demonstrably ACCEPTS AND COMPLETES work | YES | YES |

The three reserved statuses are **not producible today** — the verifier is designed but unbuilt, and per the SSRF constraint it may only ever run: owner-initiated at declaration time; scheme+host syntactic validation first; DNS-resolved address checked against private/link-local/loopback ranges; no redirects followed; one request; ≤5 s timeout; result stored, never re-probed from read paths. Arbitrary server-side probing of registered URLs from `/check`/`/search` is prohibited — that is an SSRF primitive.

## Exposed fields (per shortlist entry and per `decision`)

`reachability_status` (ladder value) · `has_declared_endpoint` (bool; replaces the earlier over-claiming `reachable` boolean) · `contact` (the declared URL or null) · `verification_method` (`declaration_only` | `declaration_probe` | `guild_observed_receipt` | null) · `last_verified_at` (ISO or null — null today, honestly) · `verification_age_seconds` (int or null) · `invocation_supported` (bool — true only under `invocation_verified`) · `recommended_for_routing` (bool — true only under `recently_reachable`/`invocation_verified`; therefore false everywhere today).

## Answer-level blocks (in `/check`)

`reachability.status = supply_has_no_declared_endpoint` — no supplier on the shortlist has any declared endpoint: honest no-route answer + `/demand/watch` alternative (never a bare recommendation).
`reachability.status = top_ranked_no_declared_endpoint` — evidence ranks an endpoint-less agent first but a declared-endpoint supplier exists: the actionable alternative is surfaced WITH its `declared_unverified` disclosure.

Field renames from the first iteration (deployed hours apart, no external consumers observed in between): `reachable` → `has_declared_endpoint`; statuses `unknown/declared_endpoint` → `no_endpoint/declared_unverified`; block statuses `supply_unreachable/top_ranked_unreachable` → `supply_has_no_declared_endpoint/top_ranked_no_declared_endpoint`.
