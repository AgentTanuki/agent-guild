# First-party token — activation runbook

2026-07-10. Mechanism already implemented in code (`app/main.py:_is_first_party`, `app/swarm/router.py:_is_first_party`): when `GUILD_FIRST_PARTY_TOKEN` is set, an `X-Guild-Source` header tags traffic first-party ONLY if it equals the token exactly. Unset (today's state), any non-empty header tags first-party — honor-based, and a third party could deliberately self-tag to hide from external metrics.

`render.yaml` now declares the env var with `sync: false`. **Setting the value in the Render dashboard is the activation switch, and tooling must be updated FIRST or our own traffic instantly degrades to external-unknown/tooling classes** (it would never pollute `genuine_external` — curl/urllib UAs are excluded — but ops dashboards would misread).

Activation order (Ross):

1. Generate a value locally (e.g. `openssl rand -hex 24`). Do not reuse the admin token.
2. Distribute to every first-party caller BEFORE setting it in Render: the ops-watch and growth-sprint scheduled-task prompts (replace "send X-Guild-Source: <anything>" with the literal token), the MCP canary (`live/scripts/mcp_canary.py` reads `GUILD_FIRST_PARTY_TOKEN` from env or `live/secrets/first_party_token`), seed/verification tooling, and any manual curl recipes in docs.
3. Set the value in Render → service env → `GUILD_FIRST_PARTY_TOKEN` → deploy.
4. Verify: a request with the OLD arbitrary header (`X-Guild-Source: test`) must now land `first_party: false`; a request with the token must land `first_party: true`; check `/instrumentation/recent`.
5. Record the activation date here and in the compliance matrix (upgrades the "internal/test/external distinguishable" item).

Notes: the token travels in a request header over TLS, same trust level as X-API-Keys. Rotation = repeat steps 1–4. The token is deliberately NOT stored in git; `live/secrets/` is gitignored.
