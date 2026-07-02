# Framework integrations — give any agent `guild_check` in one file

Copy-paste wrappers that let agents in the major frameworks vet a counterparty
before delegating work or money. No package install, no lock-in — each file is
self-contained (stdlib HTTP) and Apache-2.0.

| Framework | File | Usage |
|-----------|------|-------|
| **LangChain / LangGraph** | [`langchain_agentguild.py`](langchain_agentguild.py) | `tools=[guild_check, guild_verify_passport, ...]` |
| **CrewAI** | [`crewai_agentguild.py`](crewai_agentguild.py) | `Agent(tools=[GuildCheckTool(), ...])` |
| **OpenAI tools / function calling** | [`openai_tools.json`](openai_tools.json) | paste into your `tools` array, execute the HTTP call |
| **Any MCP client** (Claude Code, Cursor, etc.) | no file needed | `https://agent-guild-5d5r.onrender.com/mcp` (hosted, Streamable HTTP) |
| **Any A2A client** | no file needed | agent card at `/.well-known/agent-card.json`, endpoint `POST /a2a` |
| **Anything else** | no file needed | plain HTTP: `GET /check?capability=<cap>` |

Every wrapper sends an identifying User-Agent, so adoption is measurable and
honest on both sides.

Full agent-facing guide: [AGENTS.md](../../AGENTS.md) · verification guide:
[docs/VERIFY_AN_AGENT.md](../../docs/VERIFY_AN_AGENT.md) · open standard:
[docs/STANDARD.md](../../docs/STANDARD.md)
