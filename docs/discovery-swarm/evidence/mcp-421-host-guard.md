# Evidence record — production MCP 421 failure and fix

Compiled 2026-07-10 (Pilot A audit). This documents the failure exactly as observed BEFORE any edit, the fix sequence, and the security consequence of each configuration state.

## The failing production request (verbatim)

```
POST https://agent-guild-5d5r.onrender.com/mcp/ HTTP/1.1
Content-Type: application/json
Accept: application/json, text/event-stream

{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"audit","version":"1.0"}}}
```

## Complete response (identical for `/mcp` and `/mcp/`, HTTP/1.1 and HTTP/2)

```
HTTP/1.1 421 Misdirected Request
Content-Type: text/plain; charset=utf-8
Content-Length: 19
x-render-origin-server: uvicorn

Misdirected Request
```

Not machine-readable JSON — a bare text body. `x-render-origin-server: uvicorn` proves the 421 came from the application origin, not Cloudflare/Render.

## Scope of the failure

- Affected transport: the entire `/mcp` mount (streamable-HTTP). The guard runs as ASGI middleware in front of the MCP session manager, so **initialisation, tool discovery AND invocation were all blocked** — no request of any kind reached the protocol layer. Every external MCP client, including the one the official MCP registry points to, failed at the first byte. REST and A2A surfaces were unaffected (different mount).
- Duration: unknown start (introduced by whichever Render build first resolved `fastmcp>=3.4` to a guard-on-by-default release; last pre-audit deploy 2026-07-08) → fixed 2026-07-10.

## Configuration state at failure

- File/line: `live/guild/app/mcp_server.py:502` — `mcp_app = mcp.http_app(path="/")`. **No `host_origin_protection` value was configured at all**; behaviour came from the installed library's default.
- Package: `fastmcp`, requirements pin at failure: `>=3.4` (floating). Locally installed during the audit: `fastmcp==3.4.4` (+ `mcp 1.28.1`).
- Accepted values, verified by inspecting the installed package source (not memory):
  - `fastmcp/server/http.py:39` — `HostOriginProtection = bool | Literal["auto"]`; `create_streamable_http_app(..., host_origin_protection: HostOriginProtection = False)` in 3.4.4; guard raises `ValueError` unless value ∈ {True, False, "auto"}.
  - `http_app(...)` additionally accepts `allowed_hosts: list[str] | None` and `allowed_origins: list[str] | None`; passing explicit `allowed_hosts` forces host validation on (has_explicit_allowed_hosts → validate always), with `DEFAULT_HOSTS = ("127.0.0.1", "localhost", "::1")` and the bound server host always appended (`_allowed_hosts_for_scope`, http.py:309).
  - 421 source: `HostOriginGuardMiddleware.__call__`, http.py:254.
- Root cause: in 3.4.4 the default is `False` locally (no 421 reproducible with the same code), so the production build had resolved to a different fastmcp release whose default/auto behaviour validated Host and rejected the public hostname. **The unpinned dependency was the defect; the missing explicit configuration made the behaviour version-dependent.**

## Fix sequence

1. `e8749bd` (deployed 2026-07-10, emergency unblock): `host_origin_protection=False` explicit + pin `fastmcp==3.4.4`. Restored external MCP; verified in production (initialize 200, tools/list, tools/call).
2. This change (separate deploy, after the reachability deploy per clean-attribution sequencing): replace the global disable with the **narrowest supported production-safe configuration** — an explicit Host allowlist: `allowed_hosts=GUILD_PUBLIC_HOSTS` (env, default `agent-guild-5d5r.onrender.com`). Library semantics guarantee localhost/dev still works (DEFAULT_HOSTS + server host are always appended).

## Security consequence analysis

| Protection | Before failure (unpinned default) | e8749bd (`False`) | This change (allowlist) |
|---|---|---|---|
| DNS rebinding | "Protected" — by rejecting ALL legitimate public traffic (rebinding protection exists to stop browsers reaching `localhost` services via attacker DNS; it is aimed at dev servers) | None — acceptable for a public HTTPS service where Render routes by Host, so foreign-Host requests don't normally arrive at the app; defense-in-depth lost | Restored as defense-in-depth: only the declared public hosts (+ loopback for dev) reach the MCP session layer, 421 otherwise |
| Host-header injection | n/a (all rejected) | Relies on platform routing | Explicitly bounded at the app |
| Origin/CSRF | n/a | No Origin validation. Real risk is minimal: `/mcp` uses no cookies or ambient credentials, so a cross-site browser POST gains nothing; non-browser MCP clients send no Origin | Origin validation active (explicit allowlist mode): cross-site browser Origins rejected (403), same-origin and no-Origin (machine clients) pass. Strictly stronger |
| Availability risk of this change | — | — | A legitimate proxy that rewrites Host (none known; Smithery fetches origin URL directly) would 421; mitigated by env-configurable `GUILD_PUBLIC_HOSTS` and a regression test asserting the public + localhost hosts pass |

## Verification battery (results in `harness/results/mcp_battery_*.txt`)

Local (pre-deploy): original failing request replayed → 200; full handshake (initialize → notifications/initialized → tools/list → tools/call `ag_json_canonicalize`); malformed invocation → machine-readable schema error; unauthorised write (guild_attest with bad key) → machine-readable auth error; spoofed `Host: evil.example` → **421** (guard demonstrably active); FastAPI routes unaffected (`/health`, `/check`); full test suite green incl. new regression test `tests/test_mcp_host_guard.py`.

Production (post-deploy): same battery against the deployed revision + instrumentation check that the audit client classifies `AG_TEST` (UA `mcp:pilot-a-audit`), not external engagement.
