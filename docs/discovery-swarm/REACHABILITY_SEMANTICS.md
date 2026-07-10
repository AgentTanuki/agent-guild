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

The three verified statuses are **now producible** (reachability-verifier, 2026-07-10): and per the SSRF constraint it may only ever run: owner-initiated at declaration time; scheme+host syntactic validation first; DNS-resolved address checked against private/link-local/loopback ranges; no redirects followed; one request; ≤5 s timeout; result stored, never re-probed from read paths. Arbitrary server-side probing of registered URLs from any READ path (`/check`, `/search`, capability listing, journey/dashboard reads, demand matching) is prohibited and does not happen — those paths call the pure `reachability_fields()`. The verifier runs ONLY at endpoint declaration, owner-initiated.

## Exposed fields (per shortlist entry and per `decision`)

`reachability_status` (ladder value) · `has_declared_endpoint` (bool; replaces the earlier over-claiming `reachable` boolean) · `contact` (the declared URL or null) · `verification_method` (`declaration_only` | `declaration_probe` | `guild_observed_receipt` | null) · `last_verified_at` (ISO or null — null today, honestly) · `verification_age_seconds` (int or null) · `invocation_supported` (bool — true only under `invocation_verified`) · `recommended_for_routing` (bool — true only under `recently_reachable`/`invocation_verified`; therefore false everywhere today).

## Answer-level blocks (in `/check`)

`reachability.status = supply_has_no_declared_endpoint` — no supplier on the shortlist has any declared endpoint: honest no-route answer + `/demand/watch` alternative (never a bare recommendation).
`reachability.status = top_ranked_no_declared_endpoint` — evidence ranks an endpoint-less agent first but a declared-endpoint supplier exists: the actionable alternative is surfaced WITH its `declared_unverified` disclosure.

Field renames from the first iteration (deployed hours apart, no external consumers observed in between): `reachable` → `has_declared_endpoint`; statuses `unknown/declared_endpoint` → `no_endpoint/declared_unverified`; block statuses `supply_unreachable/top_ranked_unreachable` → `supply_has_no_declared_endpoint/top_ranked_no_declared_endpoint`.

## Verifier implementation (2026-07-10)

`app/reachability.py` now provides the SSRF-safe declaration-time verifier, in
three separated concerns:

1. `url_policy_check(url)` — pure, no network. Rejects a DECLARATION only for
   prohibited/invalid properties: unsupported scheme, embedded credentials,
   literal loopback/private/link-local/multicast/unspecified/reserved address,
   or a port outside {80,443,8080,8443}.
2. `liveness_probe(url)` — a single owner-initiated network check (opt-in via
   `verify=true` on `POST /agents/{id}/endpoint`). DNS-rebinding safe: resolve,
   screen EVERY resolved address, connect to a PINNED screened address, send
   HEAD with the real Host header, follow NO redirects (a 3xx is a failure),
   bound the read to 4 KB, never process the body, never send an AG secret,
   `PROBE_TIMEOUT_S=3` so a worker is not held. Yields `recently_reachable` or
   `currently_unreachable`; the declaration stands regardless.
3. `note_invocation_verified(agent_id)` (store) — the ONLY path to
   `invocation_verified`, set when a guild-observed task receipt is submitted by
   a worker that has a declared endpoint. Never set by a generic HTTP answer.

Expiry is applied in the pure `status_for()`/`reachability_fields()` read path:
`recently_reachable`/`currently_unreachable` age out after 24 h to
`declared_unverified`; `invocation_verified` after 7 days to
`declared_unverified`. `recommended_for_routing` is True only under
`recently_reachable` or `invocation_verified`.


## Refined verifier & evidence model (2026-07-10)

### Status-transition table

| status | evidence_level | entered by | expires (default, configurable) | routable |
|---|---|---|---|---|
| no_endpoint | none | no endpoint declared | — | NO |
| unknown | none | endpoint present but policy-invalid/malformed | — | NO |
| declared_unverified | none | a policy-valid URL declared (or any evidence expired / endpoint changed) | — | NO |
| verification_inconclusive | none | probe couldn't decide (capacity saturated, dup in-flight, HEAD 405 + GET inconclusive) | GUILD_REACH_HTTP_TTL (6h) | NO |
| http_responsive | http_response | a server answered (200/401/403/404/405…) but proved NO protocol | GUILD_REACH_HTTP_TTL (6h) | **NO** |
| currently_unreachable | none | connect/TLS/timeout/redirect/error | GUILD_REACH_UNREACH_TTL (24h) | NO |
| recently_reachable | protocol_handshake | protocol-specific success: A2A Agent Card, MCP initialise, or a declared health route | GUILD_REACH_RECENT_TTL (24h) | **YES** |
| invocation_verified | guild_invocation | a trusted **AG-originated** invocation to the CURRENT endpoint returned a successful protocol response, bound by a unique invocation id | GUILD_REACH_INVOCATION_TTL (7d) | **YES** |

A weak HTTP response (`http_responsive`) NEVER inherits the routing recommendation of a protocol handshake (`recently_reachable`). TTLs are env-configurable within bounded ranges.

### Exact evidence required per status

- **declared_unverified** — the agent declared a policy-valid public http(s) URL. Nobody checked it.
- **http_responsive** — an owner-initiated SSRF-safe probe got any non-redirect HTTP status from the host, but no protocol marker. Server is up; endpoint semantics unproven.
- **recently_reachable** — the probe completed a protocol handshake: A2A `/.well-known/agent-card.json` returning a card (skills/protocolVersion), or an MCP `initialize` returning a jsonrpc result. No task/credential/payload is ever sent.
- **currently_unreachable** — connection failure, TLS failure, timeout, or a refused redirect. The DECLARATION is preserved.
- **invocation_verified** — ALL of: AG initiated the invocation; it targeted the agent's current endpoint; endpoint↔agent↔invocation were bound by a unique id at invocation time; the endpoint returned a successful protocol response; the endpoint fingerprint is unchanged between invocation and verification; recorded by AG's trusted `begin/complete_outbound_invocation` path — never a submitted receipt or agent-supplied claim. (Dormant: AG has no production outbound-invocation path yet, so nothing in production produces this status.)

### Record shape
Every stored record carries: `status`, `evidence_level`, `method`, `checked_at`, `last_verified_at`, `expires_at`, `endpoint_fingerprint`, `detail` (+ `invocation_id` for invocation_verified). Changing the endpoint changes the fingerprint, which immediately invalidates all prior evidence (read paths fall back to `declared_unverified`).

### HTTPS pinning
`_connect_pinned` connects to the already-screened IP, then for https wraps TLS with `server_hostname=<original host>` (SNI) and `check_hostname=True` + `CERT_REQUIRED` (never disabled). The certificate is validated against the ORIGINAL hostname, not the IP; the HTTP layer cannot re-resolve the host (the address is fixed), so a DNS result flipping to a private IP after screening cannot redirect the connection. IPv4 and IPv6 are both screened and connected by family. Tested against a controlled self-signed HTTPS server (valid cert passes; wrong hostname and untrusted cert both fail).

### Declaration latency
`verify=true` runs the probe synchronously but **bounded** (3s timeout, ≤8KB read), concurrency-capped (`GUILD_REACH_MAX_PROBES`, default 4), per-agent rate-limited (5/60s), and deduped (identical in-flight agent+endpoint verifications collapse to `verification_inconclusive`). This synchronous design is **temporary for Pilot A** (no job system yet); the intended shape is: validate policy → save `declared_unverified` → queue verification → return a job id → update the record on completion.
