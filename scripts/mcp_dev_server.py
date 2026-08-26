"""MCP server entry point for the `mcp dev` Inspector flow.

`mcp dev` expects a Python file whose *module* exposes an attribute named
`server` (an `mcp.server.fastmcp.FastMCP` instance).  This module composes
the shared adapter server exactly the same way the `cmdb-mcp stdio`
console command does, so the Inspector talks to the very same object.
"""

from __future__ import annotations

from cmdb_mcp_adapter.config import load_settings
from cmdb_mcp_adapter.server.app import create_app

server = create_app(load_settings())
