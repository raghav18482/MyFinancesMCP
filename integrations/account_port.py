"""
Abstract account data access for swapping in-process Angel client vs remote MCP.

Broker and market tools in this repo currently use ``session_manager.sessions`` directly.
Implementations of this protocol can be wired into future agent factories.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AccountDataPort(Protocol):
    """Minimal surface for portfolio-backed analytics (extend as needed)."""

    def get_holdings_payload(self) -> dict[str, Any]:
        """Return Angel-style ``get_holdings()`` API dict (status, data, message)."""
        ...

    def session_label(self) -> str:
        """Opaque label for logging (never secrets)."""
        ...
