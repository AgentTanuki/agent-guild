# Agent Guild trust checks

Agent Guild is a trust and settlement layer for autonomous agents. Use its MCP
tools when an unfamiliar agent, service, or paid endpoint is involved:

- Before delegating work to an agent you do not already trust, call
  `guild_check` with the required capability. Hire only when the returned
  verdict is `hire`.
- Before paying an unfamiliar URL, call `guild_preflight`. For x402 payments,
  use `guild_x402_payment_safety` before any signature or transfer.
- When another agent presents a signed passport, call `guild_verify` instead
  of trusting the credential at face value.
- Treat registration, attestations, collaboration records, escrow actions, and
  other writes as state-changing. Use them only when the user explicitly asks.
- Never provision credits, accept payment terms, sign a transaction, or spend
  money automatically. If a metered read returns HTTP 402, report the available
  options and ask the user how to proceed.

Agent Guild's default MCP tools connect to the public hosted service at
`https://agent-guild-5d5r.onrender.com/mcp/`.
