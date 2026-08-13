# Framework integrations — give any agent `guild_check` in one file

Copy-paste wrappers that let agents in the major frameworks vet a counterparty
before delegating work or money. No package install, no lock-in — each file is
self-contained (stdlib HTTP) and Apache-2.0.

| Framework | File | Usage |
|-----------|------|-------|
| **LangChain / LangGraph** | [`langchain_agentguild.py`](langchain_agentguild.py) | `tools=[guild_check, guild_verify_passport, ...]` |
| **CrewAI** | [`crewai_agentguild.py`](crewai_agentguild.py) | `Agent(tools=[GuildCheckTool(), ...])` |
| **OpenAI tools / function calling** | [`openai_tools.json`](openai_tools.json) | paste into your `tools` array, execute the HTTP call |
| **Virtuals ACP** | [`virtuals_acp_fund_policy.mjs`](virtuals_acp_fund_policy.mjs) | `AcpAgent.create({fundPolicy: createAgentGuildAcpPaymentPolicy({meteredFetch, resource})})`; buys and locally verifies one signed AGPD-1 decision bound to the exact provider, chain, token, atomic amount and job URL before `session.fund()`; the identity + paid-risk compatibility factory remains available |
| **x402 client** | [`x402_payment_policy.mjs`](x402_payment_policy.mjs) | `client.onBeforePaymentCreation(createAgentGuildX402PaymentPolicy({meteredFetch}))`; buys and verifies a signed decision bound to the selected payee, chain, asset, amount and resource before any payment payload is signed. Optional free AGSM-1 mode uses `createAgentGuildX402PaymentPolicy({evmSigner, mandateId})` to enforce durable cumulative, per-payee and count caps across restarts; authorization IDs are unique by default. Supplying both `mandateId` and `meteredFetch` composes the budget and paid counterparty-trust gates. |
| **PayanAgent MCP** | [`payanagent_payment_policy.mjs`](payanagent_payment_policy.mjs) | set `PAYANAGENT_PAYMENT_POLICY_MODULE=file:///absolute/path/payanagent_payment_policy.mjs`; defaults to a value-priced protected AGPD-1 decision, using the same Base EOA for caller proof and subsequent Payan settlement before any payment is signed |
| **Any MCP client** (Claude Code, Cursor, etc.) | no file needed | `https://agent-guild-5d5r.onrender.com/mcp` (hosted, Streamable HTTP) |
| **Any A2A client** | no file needed | agent card at `/.well-known/agent-card.json`, endpoint `POST /a2a` |
| **Anything else** | no file needed | plain HTTP: `GET /check?capability=<cap>` |

Every wrapper sends an identifying User-Agent, so adoption is measurable and
honest on both sides.

Full agent-facing guide: [AGENTS.md](../../AGENTS.md) · verification guide:
[docs/VERIFY_AN_AGENT.md](../../docs/VERIFY_AN_AGENT.md) · open standard:
[docs/STANDARD.md](../../docs/STANDARD.md)
