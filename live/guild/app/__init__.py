"""Agent Guild live service package."""

# Single source of truth for the service version. Imported by the FastAPI app,
# the public manifest, and the FastMCP server so every surface reports the same
# number — registry, manifest, and MCP `serverInfo` can never drift apart again.
__version__ = "1.2.0"   # corrective integrity pass 2026-07-13 (AGD-1 binding,
                        # verify-before-use, AGO-1 outcomes, hashed-key
                        # settlement, checkpoint inclusion proofs)
