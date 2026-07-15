"""Agent Guild live service package."""

# Single source of truth for the service version. Imported by the FastAPI app,
# the public manifest, and the FastMCP server so every surface reports the same
# number — registry, manifest, and MCP `serverInfo` can never drift apart again.
__version__ = "2.0.0"   # DELIBERATE MAJOR BUMP (machine-visible semver,
                        # 2026-07-15): payment enforcement on previously-free
                        # MCP tools (guild_check/guild_search/guild_best_agent/
                        # guild_risk_score) and the A2A `check` skill shipped
                        # AFTER 1.2.0 was already published to the MCP
                        # Registry. For a machine consumer "this call now
                        # returns a payment challenge instead of the result"
                        # is a breaking contract change; it must never reuse a
                        # published version. 1.x listings describe the
                        # pre-enforcement behaviour; 2.0.0 declares the x402
                        # payment mechanism + priced operations in its
                        # publisher-provided registry metadata.
                        # (Also in 2.0.0: x402 offer/receipt did:web service
                        # identity + durable payment crash recovery.)
