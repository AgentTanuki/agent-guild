# Branch notes — reachability-verifier

Concern (ONE): the SSRF-safe declaration-time reachability verifier that
advances declared_unverified -> recently_reachable / currently_unreachable /
invocation_verified (the statuses reserved in REACHABILITY_SEMANTICS.md).
Nothing else.

## Owns (may modify)
- live/guild/app/reachability.py — add the verifier + producing the reserved
  statuses + populating verification_method / last_verified_at /
  verification_age_seconds.
- live/guild/app/store.py — ONLY set_agent_endpoint (record the verification
  result at declaration time) and the reachability-field emitter it calls.
  MUST NOT touch persistence internals or credential/keying logic.
- live/guild/app/main.py — ONLY declare_endpoint (invoke the verifier).
  MUST NOT touch _require_key / scope enforcement.
- Tests: tests/test_reachability_verifier.py (new).
- docs/discovery-swarm/REACHABILITY_SEMANTICS.md (mark statuses producible).

## SSRF constraints (hard)
- Owner-initiated at declaration time ONLY; never from read paths (/check,
  /search must never trigger a fetch).
- scheme in {http,https}; reject non-URL. Resolve DNS; reject if the resolved
  IP is private/loopback/link-local/reserved. No redirects. One request.
  <=5s timeout. Store result; never re-probe from reads.

## Must NOT touch
- persistence engine -> sqlite-persistence
- credentials.py / auth / scopes -> credential-hardening
- generated state / production data / secrets / evidence files

## Flag / safety
- Verifier is opt-in per declaration; recommended_for_routing stays false unless
  a status in {recently_reachable, invocation_verified} is actually produced.

## Cross-branch dependency
- store.py (set_agent_endpoint region) + main.py (declare_endpoint) overlap with
  credential-hardening's store.py/main.py regions (different functions).
  Integrate SECOND (after credentials, before sqlite).

## Status
- New branch from main 2f5b60a. Not yet built.
