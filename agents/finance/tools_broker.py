"""
Angel One broker tools for ADK, aligned with MCP server capabilities.

Tools close over ``session_id`` (in-memory Angel session from session_manager).
Do not pass secrets through the LLM; create the session in the runner first.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from session_manager import sessions
from services.broker_service import (
    calculate_symbol_pnl,
    cancel_order_result,
    fetch_market_depth,
    fetch_stock_history_candles,
    holdings_for_sector_analysis,
    modify_order_result,
    place_order_result,
    portfolio_summary_as_dict,
)

logger = logging.getLogger(__name__)

_MAX_HISTORY_CANDLES = 40


def _client_or_error(session_id: str):
    c = sessions.get_client(session_id)
    if c is None:
        return None
    return c


def _not_logged_in() -> dict[str, Any]:
    return {
        "error": "No Angel One session. Log in from the runner (sessions.create_session) first.",
    }


def make_broker_tools(session_id: str) -> list[Callable[..., Any]]:
    """Build broker tool callables bound to the given Angel session id."""

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

    def angel_place_order(
        variety: str,
        tradingsymbol: str,
        symboltoken: str,
        transactiontype: str,
        exchange: str,
        ordertype: str,
        producttype: str,
        quantity: str,
        price: str = "0",
        triggerprice: str = "0",
        duration: str = "DAY",
    ) -> dict[str, Any]:
        """Place a live order. Confirm all parameters with the user before calling."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        order_params = {
            "variety": variety,
            "tradingsymbol": tradingsymbol,
            "symboltoken": symboltoken,
            "transactiontype": transactiontype,
            "exchange": exchange,
            "ordertype": ordertype,
            "producttype": producttype,
            "duration": duration,
            "price": price,
            "triggerprice": triggerprice,
            "quantity": quantity,
        }
        logger.info("place_order %s", json.dumps(order_params, default=str))
        return place_order_result(client, order_params)

    def angel_modify_order(
        variety: str,
        orderid: str,
        ordertype: str,
        quantity: str,
        price: str,
        triggerprice: str = "0",
        producttype: str = "DELIVERY",
        duration: str = "DAY",
    ) -> dict[str, Any]:
        """Modify an open order."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        order_params = {
            "variety": variety,
            "orderid": orderid,
            "ordertype": ordertype,
            "producttype": producttype,
            "duration": duration,
            "price": price,
            "triggerprice": triggerprice,
            "quantity": quantity,
        }
        logger.info("modify_order %s", json.dumps(order_params, default=str))
        return modify_order_result(client, order_params)

    def angel_cancel_order(orderid: str, variety: str = "NORMAL") -> dict[str, Any]:
        """Cancel an open order by id."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        logger.info("cancel_order orderid=%s variety=%s", orderid, variety)
        return cancel_order_result(client, orderid, variety)

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
        angel_place_order,
        angel_modify_order,
        angel_cancel_order,
    ]
