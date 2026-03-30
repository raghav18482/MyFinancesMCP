"""
Remote MCP account bridge (stub).

To route the agent through a running FastMCP server instead of in-process ``AngelOneClient``,
use the official MCP client with the same transport your IDE uses (often stdio or SSE).
``MCP_ACCOUNT_SERVER_URL`` is reserved for HTTP-based MCP endpoints when you add a concrete
implementation.

This module intentionally does not ship a full client: transports and auth vary by deployment.
"""
from __future__ import annotations

import os
from typing import Any


def _mcp_server_url() -> str | None:
    u = (os.environ.get("MCP_ACCOUNT_SERVER_URL") or "").strip()
    return u or None


class McpAccountBridge:
    """Placeholder for MCP-backed account access."""

    def __init__(self, server_url: str | None = None):
        self._url = server_url or _mcp_server_url()

    def session_label(self) -> str:
        return f"mcp:{self._url or 'unconfigured'}"

    def get_holdings_payload(self) -> dict[str, Any]:
        raise NotImplementedError(
            "McpAccountBridge.get_holdings_payload is not implemented. "
            "Use LocalAngelAccountAdapter with session_manager, or implement MCP client calls "
            "(e.g. mcp.ClientSession with your server's transport). "
            f"Configured URL hint: {self._url!r}"
        )
