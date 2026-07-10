# Branch notes — credential-hardening

Concern (ONE): credential storage & authorization. Nothing else.

## Owns (may modify)
- live/guild/app/credentials.py (new; sole owner)
- live/guild/app/store.py — ONLY credential regions: register_agent key issuance,
  account keying by key_id, event-actor-key sanitisation, auth/verify helpers.
  MUST NOT touch persistence internals (_load/_save/journal) or set_agent_endpoint.
- live/guild/app/swarm/gateway.py — ONLY derive_actor credential lookup.
- live/guild/app/mcp_server.py — ONLY auth in guild_attest/guild_record/_prove_auth.
- live/guild/app/main.py — ONLY _require_key + per-route scope enforcement.
  MUST NOT touch declare_endpoint.
- Tests: test_credential_hardening.py + mode-aware edits to
  test_credential_lifecycle.py, test_phase1_journey.py.
- docs/discovery-swarm/CREDENTIALS_DESIGN.md.

## Must NOT touch
- persistence engine (_load/_save/journal) -> sqlite-persistence
- reachability.py, set_agent_endpoint, declare_endpoint -> reachability-verifier
- generated state (guild.json/*.events.jsonl), production data, secrets,
  docs/discovery-swarm/evidence/*

## Flag / safety
- GUILD_HASH_KEYS default OFF = byte-for-byte current behavior. Activation is
  irreversible (one-way hash migration) and is a SEPARATE operator step, not
  part of merge/deploy.

## Cross-branch dependency
- store.py and main.py are also touched by other branches (different regions).
  Integration order: credential-hardening FIRST, then reachability-verifier,
  then sqlite-persistence.

## Status
- Base d702db5: 297 tests pass both modes on its OLD base. Needs rebase onto
  current main (store.py reachability/analytics changes) before review/merge.
