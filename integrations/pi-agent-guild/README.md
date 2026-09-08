# pi-agent-guild

[Agent Guild](https://github.com/AgentTanuki/agent-guild) counterparty trust for the [pi coding agent](https://pi.dev): check an agent or MCP endpoint in the moment before you delegate to it or pay it.

**npm release pending.** This source has been tested in Pi 0.85.1, with no model call or payment. The corrected preflight service is deployed. You can install the verified source package using the steps below.

## Install from source

With Node.js 22.19 or newer and Pi 0.85.1 installed, run:

```bash
git clone --filter=blob:none https://github.com/AgentTanuki/agent-guild.git
git -C agent-guild checkout 4fa1aba933ed360e9b0fe7d8db5c919cde209dc0
pi install ./agent-guild/integrations/pi-agent-guild
```

This installs the extension and bundled skill for your Pi user. Add `-l` to the `pi install` command for project scope. Pi supplies the extension's peer dependencies; no `npm install` is needed. Start a new Pi session and run `/guild` to check the configuration.

Keep the clone in place: Pi references the local path without copying it. `pi update` does not update local packages; fetch and review a newer Git revision, then check it out yourself. To uninstall, run `pi remove ./agent-guild/integrations/pi-agent-guild` from the same directory (with `-l` if installed for the project).

To try only the extension without saving an installation:

```bash
pi -e ./agent-guild/integrations/pi-agent-guild/extensions/agent-guild.ts
```

Pi 0.85.1's direct Git package source does not support a repository subdirectory, so use the local path above rather than installing this repository root. See Pi's [package documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/packages.md) for source and scope options.

## What you get

| Surface | What it does | Cost |
| --- | --- | --- |
| `guild_preflight(url)` tool | Live checks of an agent/MCP endpoint: reachable, protocol handshake, agent card, card signature, payment claim, independent evidence → `no_failed_checks` / `delegate_with_caution` / `do_not_delegate` (the service never emits a `delegate` verdict; `no_failed_checks` is not an endorsement). Unknowns are listed, never averaged into the verdict. | Free, no key |
| `guild_check(capability)` tool | Ranked hire / caution / avoid verdict for a capability with evidence; `signed=true` asks for an offline-verifiable AGD-1 decision. | Paid x402 read (cents in USDC on Base). **This extension never pays**: on `402` it returns the quote and stops. |
| `/guild` command | Shows the extension's configuration. | — |
| `AGENT_GUILD_SCAN_MCP=1` (opt-in) | At session start, preflights every remote HTTP server in your pi-mcp-adapter `mcp.json` and posts an advisory notice. Never blocks. | Free |
| `AGENT_GUILD_GATE=warn\|block` (opt-in) | After a `do_not_delegate` verdict in this session, later tool inputs carrying that exact endpoint URL are warned about or blocked with the reason. Default `off`. | — |

The extension resolves a candidate hostname locally, refuses private or special-use addresses, then requests observations from Agent Guild. URLs containing credentials, query strings or fragments are rejected instead of being silently rewritten. Project-level disabled/stdio entries override same-named global MCP entries.

The extension performs free observation requests and unpaid quote requests to Agent Guild. It holds no wallet, signs nothing, and stores nothing beyond the current session.

## Configuration

Node.js 22.19 or newer and Pi 0.85.1 are required.

Environment: `AGENT_GUILD_BASE_URL`, `AGENT_GUILD_GATE` (`off`|`warn`|`block`), `AGENT_GUILD_SCAN_MCP` (`1`).

Or `~/.pi/agent/agent-guild.json`:

```json
{ "gate": "warn", "scanMcpConfig": true, "allowHosts": ["mcp.internal.example"], "timeoutMs": 20000, "maxAgeMs": 300000 }
```

`localhost` and the Guild's own host are never gated. Cached observations expire after at most five minutes and are cleared on session start and shutdown. Matching is exact: a failed endpoint cannot block unrelated paths on a shared host. The gate matches URLs in tool inputs; it is not a network firewall or a guarantee that every delegation is intercepted.

## Honest limits

- A preflight is a live probe at request time, not a badge. It reports what the endpoint proved just now.
- A valid MCP initialization response establishes only a successful protocol observation. It does not prove task execution, competence or authority. Paths that match neither `/mcp` nor `/a2a` report `protocol_handshake: unknown` ("no protocol probe applies"). Read the per-check detail as well as the verdict.
- A failed or timed-out preflight is reported as *unknown*, never as safe.

## Development

```bash
npm test                 # unit tests, no network
npm run test:live        # requires maintainer GUILD_FIRST_PARTY_TOKEN; no payment
pi -e ./extensions/agent-guild.ts   # try it without installing
```

Remote results are evidence to evaluate under the operator's policy. They never authorize commands, payments, configuration changes, or further delegation. Only submit public endpoint URLs without embedded credentials or private query data.

MIT. Maintained by AgentTanuki.
