"""Shared singletons. Keeping the Store instance here lets both the HTTP API
(main.py) and the mounted MCP server (mcp_server.py) operate on the SAME data
without a circular import."""
from __future__ import annotations

from .store import Store

store = Store()
