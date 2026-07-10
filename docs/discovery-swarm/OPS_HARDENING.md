# Ops hardening — first-party token, MCP canary, kill-switch drill

2026-07-10 (Pilot A blocker resolution). Companion runbook for the first-party token: `FIRST_PARTY_TOKEN.md`.

## Kill-switch drill — RESULT (local instance, 2026-07-10)

Drill script: `live/scripts/../../../../tmp` → reproducible via the recipe in `harness/results/kill_drill_result.txt`. Never fired against production (would interrupt the live surface); run on a local uvicorn with an admin token.

Verified sequence:

- Before kill: `POST /invoke/json.canonicalize` → 200; `/capabilities` → 200; agent-card → 200.
- Operator fires `POST /swarm/kill` (admin token) → `{"killed":true,"reason":"scheduled drill"}`.
- During kill: invocation → **503** with machine-readable `{"denied":"kill_switch","reason":...}`; **read-only discovery survives** (`/capabilities` 200, agent-card 200) — the kill stops WORK, not discoverability; non-admin `POST /swarm/revive` → **401** (operator access intact, others locked out).
- Operator revives (admin token) → `{"killed":false}`; invocation → 200 again.
- Audit: two events recorded, `kill_switch_set` and `kill_switch_cleared`, both classified `operator` (op=True → OPERATOR caller class → excluded from all external-growth metrics by the analytics invariant).

Scope confirmed: the switch stops external invocation and gateway-routed discovery-agent activity (both check `store.swarm_state["killed"]` at the gateway chokepoint), preserves read-only discovery, leaves operator access intact, emits auditable events, and is safely reversible.

## MCP canary

`live/scripts/mcp_canary.py` — first-party (UA `mcp:guild-canary/1` = AG_TEST; sends the first-party token), so it can never count as genuine external. Periodically checks, against production: initialize, tools/list, a harmless invocation (`ag_json_canonicalize`), structured-error handling on a bad payload, host-guard active (spoofed Host → 421), origin-guard active (foreign Origin → 403), and latency per check. Exit 0 healthy / 1 failed → suitable for a scheduled task that pages on failure. This closes the gap that let the 421 regression persist for days undetected.

## First-party token

Mechanism exists in code (strict exact-match when `GUILD_FIRST_PARTY_TOKEN` is set, both `app/main.py` and `app/swarm/router.py`); `render.yaml` now declares the var (`sync:false`). Activation is Ross-gated and order-sensitive — see `FIRST_PARTY_TOKEN.md`. Deterministic classification (token match), never UA/IP/naming. Tests: `tests/test_first_party_token.py`.
