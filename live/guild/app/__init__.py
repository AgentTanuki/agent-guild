"""Agent Guild live service package."""

# Single source of truth for the service version. Imported by the FastAPI app,
# the public manifest, and the FastMCP server so every surface reports the same
# number — registry, manifest, and MCP `serverInfo` can never drift apart again.
__version__ = "1.1.0"
