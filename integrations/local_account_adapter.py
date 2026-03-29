"""
In-process Angel One client bound to a ``session_manager`` session id.
"""
from __future__ import annotations

from typing import Any

from session_manager import sessions


class LocalAngelAccountAdapter:
    """Implements :class:`integrations.account_port.AccountDataPort` via ``AngelOneClient``."""

    def __init__(self, angel_session_id: str):
        self._sid = angel_session_id

    def session_label(self) -> str:
        return self._sid[:8] + "…" if len(self._sid) > 8 else self._sid

    def get_holdings_payload(self) -> dict[str, Any]:
        client = sessions.get_client(self._sid)
        if client is None:
            return {"status": False, "message": "No Angel session", "data": None}
        return client.get_holdings()
