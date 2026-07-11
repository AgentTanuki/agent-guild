# Pilot B Entry — status

Step 1 (first-party authentication) is the ONLY Pilot B entry action in progress. No SQLite cutover, no credential hashing, no registry refresh, no fact-check publish, no swarm launch, no contact with `a2a:net:4580505b`, no external-facing capability change.

## Step 1 — first-party authentication: **PREPARED; production token PENDING Ross**

Server-side mechanism, caller updates, and tests are complete and merged-ready (dark: unchanged until the token is set). Setting `GUILD_FIRST_PARTY_TOKEN` in Render is the one operator action that activates strict mode. Until then, honor mode (any non-empty first-party header → first-party) is preserved, so production behaviour is unchanged.

Step 1 is COMPLETE only when: the production token is set, AG-owned callers use it, actual production events classify correctly, and external metrics are unchanged. Current state: **awaiting the Render secret action.**

## First-party caller inventory

| Caller | Script / source | Auth today | UA | Prod routes | Classification (after activation) | Runs | Token source |
|---|---|---|---|---|---|---|---|
| MCP canary | `live/scripts/mcp_canary.py` | UA + (now) dedicated header | `mcp:guild-canary/1` | `/mcp/` | **AG_TEST** (UA; role=test) | scheduled (to be) | `GUILD_FIRST_PARTY_TOKEN` env or `live/secrets/first_party_token`; sends none if absent (still AG_TEST by UA) |
| ops-watch | scheduled task (prompt) | `X-Guild-Source` + `guild-ops-check` UA | `guild-ops-check` | GET reads, `/instrumentation` | AG_TEST (UA) → **AG_INTERNAL** if it sends role=internal | schedule 07:06 | prompt carries the token header |
| growth-sprint | scheduled task (prompt) | `X-Guild-Source` | curl/urllib | GET reads, `/a2a` | **AG_INTERNAL** (fp + role internal) | schedule 10:05 | prompt carries the token header |
| self-eval tick | `live/scripts/self_eval_tick.py` | admin token → `op` | urllib | `/self-eval/run` | **OPERATOR** (admin token) | scheduled | admin token (unchanged) |
| reachability probe | `app/reachability.py` (`guild-reachability-probe/1`) | n/a (outbound, never inbound to us) | `guild-reachability-probe/1` | outbound only | **AG_TEST** if it ever appears inbound (UA) | on endpoint declare | n/a |
| verify_mcp | `live/scripts/verify_mcp.py` | interactive verification | urllib | `/mcp/` | AG_TEST if it adopts the canary UA/token | interactive | env token |
| seed / bootstrap | `live/scripts/seed_supply.py` | admin/seed | urllib | `/agents/register` (seed) | seed → first-party via admin path | interactive | admin token |
| detect_external / recruit_scout / first_contact / onboard_demo / outreach | `live/scripts/*.py` | mixed | mixed | reads | interactive/one-off; **must send the token header** if run against prod, else fail closed | interactive | env token |

Secret values are never printed here.

## Server-side classification order (implemented)

1. **valid first-party token** (constant-time; dedicated `X-Agent-Guild-First-Party`, legacy `X-Guild-Source` accepted) → first-party. Role header (`X-Agent-Guild-Role: test|internal`, default internal) or a test UA decides **AG_TEST vs AG_INTERNAL**.
2. authenticated external member (valid api key) → EXTERNAL_MEMBER / EXTERNAL_VERIFIED.
3. registry crawler (UA) → REGISTRY_CRAWLER.
4. known AG test/verification UA (defense-in-depth, even without the token) → AG_TEST.
5. else → EXTERNAL_UNKNOWN.

An **invalid or missing token in strict mode never sets first-party**, so it can never produce AG_INTERNAL. UA/IP/naming are defense-in-depth only, never the primary trust. A valid token grants **no** scopes and **no** admin authority. The raw token is never recorded — only whether first-party auth succeeded, the caller class, the caller id, the route, and a timestamp.

## Operator action (Ross) — exact steps

The token must reach AG-owned callers **before or atomically with** the server, so no job falls back into external metrics. I cannot set the Render secret; this is the approval boundary.

1. **Generate** (locally; never paste into chat, logs, git, or docs):
   ```
   openssl rand -hex 24        # 192 bits, cryptographically secure
   ```
   Store it in your password manager. Do not reuse `GUILD_ADMIN_TOKEN`.
2. **Distribute to callers first:**
   - Put the value in `live/secrets/first_party_token` on the box that runs the canary/verification scripts (gitignored), or set `GUILD_FIRST_PARTY_TOKEN` in that environment.
   - Update the ops-watch and growth-sprint **scheduled-task prompts** to send the header on every production call: `X-Agent-Guild-First-Party: <token>` (ops-watch also `X-Agent-Guild-Role: internal`). (I can update these prompts via the scheduler on your say-so.)
3. **Confirm callers ready** (dry-run the canary locally; it should send the header and still pass).
4. **Set in Render:** service → Environment → `GUILD_FIRST_PARTY_TOKEN` = value.
5. **Redeploy** (single instance stop-then-start).
6. **Verify** (I run this after you confirm the secret is set — see below).
7. **Rotate / rollback** if needed (procedure below).

## Post-activation verification (I run this after Ross sets the secret)

Capture before/after `/instrumentation`, then:
- one MCP canary cycle → its events classify **AG_TEST**;
- one ops-watch-style internal read with the token + role=internal → **AG_INTERNAL**;
- one AG test probe → **AG_TEST**;
- one unauthenticated external-style probe → **not first-party**.
Prove: genuine-external total does not rise from first-party calls; AG_TEST/AG_INTERNAL rise by the exact expected number; no external registration/referral/repeat/transaction metric changes; production healthy; MCP functional; A2A and `/check` unchanged.

## Rotation procedure (no permanent multi-token ambiguity)

1. Generate a **new** token.
2. Update all AG-owned callers to send the new token.
3. **Optional dual-token window:** set `GUILD_FIRST_PARTY_TOKEN_PREV = <old>` while callers migrate (both accepted, constant-time). Keep this window short.
4. Set `GUILD_FIRST_PARTY_TOKEN = <new>` in Render; redeploy.
5. Once all callers use the new token, **remove `GUILD_FIRST_PARTY_TOKEN_PREV`**; redeploy.
6. **Verify the old token now fails** (`is_first_party(old)` → false; an old-token call classifies not-first-party).
7. Remove transitional support. Do not leave both tokens configured permanently.

## Rollback

Unset `GUILD_FIRST_PARTY_TOKEN` (and `_PREV`) and redeploy → returns to honor mode (pre-activation behaviour). Non-destructive; classification is read-time. No data change.

## Unresolved risks / notes

- Honor mode remains the pre-activation default (any non-empty first-party header tags first-party) so current jobs keep working until the token is set; the "missing token never AG_INTERNAL" guarantee holds once activated. This is deliberate and reversible.
- The ops-watch/growth-sprint prompt updates are outside the repo (scheduler); they must be updated in lockstep with the Render secret or those jobs briefly classify by UA/tooling (still never genuine-external — curl/urllib is tooling, guild-ops-check is AG_TEST).
- Historical genuine-external events are NOT reclassified by the token (it applies prospectively); the read-time defense-in-depth UA rules for known probes remain, but **token authentication is now authoritative** going forward.
