"""
Read-only Angel One broker tools for ADK, aligned with MCP server capabilities.

Tools close over ``session_id`` (in-memory Angel session from session_manager)
and only ever hold a :class:`services.readonly_broker.ReadOnlyBroker`, which has
no order methods. Do not pass secrets through the LLM; the host app creates the
session before the agent runs.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from services.broker_service import (
    calculate_symbol_pnl,
    fetch_market_depth,
    fetch_stock_history_candles,
    holdings_for_sector_analysis,
    portfolio_summary_as_dict,
)
from services.readonly_broker import readonly_client

logger = logging.getLogger(__name__)

_MAX_HISTORY_CANDLES = 40


def _client_or_error(session_id: str):
    return readonly_client(session_id)


def _not_logged_in() -> dict[str, Any]:
    return {
        "error": "No Angel One session. Log in from the runner (sessions.create_session) first.",
    }


def make_broker_tools(session_id: str) -> list[Callable[..., Any]]:
    """Build read-only broker tool callables bound to the given Angel session id."""

    def angel_get_profile() -> dict[str, Any]:
        """Get Angel One account profile (name, client ID, email, exchanges, products)."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_profile()

    def angel_get_holdings() -> dict[str, Any]:
        """List demat holdings with quantity, average price, LTP, and P&L."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_holdings()

    def angel_get_all_holdings() -> dict[str, Any]:
        """Aggregated holdings totals: total investment, current value, overall P&L."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_all_holdings()

    def angel_get_positions() -> dict[str, Any]:
        """Open intraday/delivery positions and day P&L."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_positions()

    def angel_get_order_book() -> dict[str, Any]:
        """Today's orders with status, price, quantity."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_order_book()

    def angel_get_trade_book() -> dict[str, Any]:
        """Today's executed trades."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_trade_book()

    def angel_get_funds() -> dict[str, Any]:
        """Funds / margin snapshot (available cash, net, etc.)."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_funds()

    def angel_get_ltp(exchange: str, tradingsymbol: str, symboltoken: str) -> dict[str, Any]:
        """Last traded price for an instrument (needs exchange, symbol, token)."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_ltp(exchange, tradingsymbol, symboltoken)

    def angel_search_scrip(exchange: str, search_text: str) -> dict[str, Any]:
        """Search instruments on an exchange; use to resolve symbol tokens."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.search_scrip(exchange, search_text)

    def angel_portfolio_summary() -> dict[str, Any]:
        """Structured summary: holdings totals, day P&L from positions, key fund fields."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return portfolio_summary_as_dict(client)

    def angel_holdings_for_sectors() -> dict[str, Any]:
        """Holdings rows formatted for sector analysis (symbol, current value, P&L)."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        rows = holdings_for_sector_analysis(client)
        return {"holdings": rows, "count": len(rows)}

    def angel_get_candle_data(
        exchange: str,
        symboltoken: str,
        interval: str,
        fromdate: str,
        todate: str,
    ) -> dict[str, Any]:
        """OHLCV candles for explicit date range (Angel API format dates)."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        params = {
            "exchange": exchange,
            "symboltoken": symboltoken,
            "interval": interval,
            "fromdate": fromdate,
            "todate": todate,
        }
        return client.get_candle_data(params)

    def angel_get_stock_history(
        symbol: str,
        days: int = 30,
        interval: str = "ONE_DAY",
    ) -> dict[str, Any]:
        """Historical OHLCV by symbol name; returns last rows only to limit context size."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        raw = fetch_stock_history_candles(client, symbol, days, interval)
        if not raw.get("ok"):
            return raw
        candles = raw.get("candles") or []
        tail = candles[-_MAX_HISTORY_CANDLES:]
        out = {k: v for k, v in raw.items() if k != "candles"}
        out["candles"] = tail
        out["candles_returned"] = len(tail)
        out["candles_total"] = len(candles)
        out["candles_truncated"] = len(candles) > len(tail)
        return out

    def angel_calculate_pnl(symbol: str) -> dict[str, Any]:
        """P&L from demat holding plus today's positions for a symbol."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return calculate_symbol_pnl(client, symbol)

    def angel_get_market_depth(symbol: str, exchange: str = "NSE") -> dict[str, Any]:
        """Full market depth (bids/asks) and quote header for a symbol."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return fetch_market_depth(client, symbol, exchange)

    return [
        angel_get_profile,
        angel_get_holdings,
        angel_get_all_holdings,
        angel_get_positions,
        angel_get_order_book,
        angel_get_trade_book,
        angel_get_funds,
        angel_get_ltp,
        angel_search_scrip,
        angel_portfolio_summary,
        angel_holdings_for_sectors,
        angel_get_candle_data,
        angel_get_stock_history,
        angel_calculate_pnl,
        angel_get_market_depth,
    ]
