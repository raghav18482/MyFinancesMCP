"""
The only broker handle AI-facing code is allowed to hold.

``ReadOnlyBroker`` wraps an in-memory ``AngelOneClient`` and exposes an explicit
allowlist of read methods. There is no ``__getattr__`` passthrough, so anything
not listed here — order endpoints, the SmartConnect object, credentials — is an
``AttributeError`` rather than a reachable capability. MCP tools and ADK agent
tools obtain clients only through :func:`readonly_client` or :func:`wrap`.

This is a capability boundary, not a sandbox: Python cannot hide the wrapped
object from code that goes looking for it. What it guarantees is that tool code
written against this interface, and anything a model can ask that code to do,
has no route to a mutation.
"""
from __future__ import annotations

from typing import Any, Optional

READ_ONLY_BROKER_METHODS: frozenset[str] = frozenset({
    "get_profile",
    "get_holdings",
    "get_all_holdings",
    "get_positions",
    "get_order_book",
    "get_trade_book",
    "get_funds",
    "get_ltp",
    "search_scrip",
    "get_candle_data",
    "get_market_data",
})


class ReadOnlyBroker:
    """Allowlisted, read-only view of a broker client."""

    __slots__ = ("__client",)

    def __init__(self, client: Any) -> None:
        if isinstance(client, ReadOnlyBroker):
            client = client.__client
        self.__client = client

    def __repr__(self) -> str:
        return f"<ReadOnlyBroker client_id={self.client_id!r}>"

    @property
    def client_id(self) -> str:
        return self.__client.client_id

    @property
    def client_code(self) -> str:
        return getattr(self.__client, "client_code", None) or self.__client.client_id

    def get_profile(self) -> dict:
        return self.__client.get_profile()

    def get_holdings(self) -> dict:
        return self.__client.get_holdings()

    def get_all_holdings(self) -> dict:
        return self.__client.get_all_holdings()

    def get_positions(self) -> dict:
        return self.__client.get_positions()

    def get_order_book(self) -> dict:
        return self.__client.get_order_book()

    def get_trade_book(self) -> dict:
        return self.__client.get_trade_book()

    def get_funds(self) -> dict:
        return self.__client.get_funds()

    def get_ltp(self, exchange: str, tradingsymbol: str, symboltoken: str) -> dict:
        return self.__client.get_ltp(exchange, tradingsymbol, symboltoken)

    def search_scrip(self, exchange: str, search_text: str) -> dict:
        return self.__client.search_scrip(exchange, search_text)

    def get_candle_data(self, params: dict) -> dict:
        return self.__client.get_candle_data(params)

    def get_market_data(self, mode: str, exchange_tokens: dict) -> dict:
        return self.__client.get_market_data(mode, exchange_tokens)


def wrap(client: Any) -> Optional[ReadOnlyBroker]:
    """``ReadOnlyBroker`` around ``client``, or None for None."""
    if client is None:
        return None
    return ReadOnlyBroker(client)


def readonly_client(session_key: str) -> Optional[ReadOnlyBroker]:
    """The read-only broker for an in-memory session, or None if there is none."""
    from session_manager import sessions

    if not session_key:
        return None
    return wrap(sessions.get_client(session_key))
