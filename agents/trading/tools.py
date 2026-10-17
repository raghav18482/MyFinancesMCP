"""
Trading-analysis agent tools — read-only, with hypothetical trade ideas only.

Tools close over ``session_id`` for broker data access and only ever hold a
:class:`services.readonly_broker.ReadOnlyBroker`. Nothing here can place,
modify or cancel an order, or create anything that later could:
``trading_hypothetical_trade_idea`` returns an analysis dict and stores nothing.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from services.broker_service import (
    fetch_market_depth,
    fetch_stock_history_candles,
)
from services.readonly_broker import readonly_client
from services.risk_profile import risk_profiles, load_profile
from services.trade_proposals import proposal_store, validate_against_profile
from services.technical_service import compute_technical_indicators

logger = logging.getLogger(__name__)

_MAX_HISTORY_CANDLES = 40


HYPOTHETICAL_LABEL = "Hypothetical — not executed"

HYPOTHETICAL_DISCLAIMER = (
    "This is a hypothetical idea for analysis only. It has not been and cannot be "
    "executed by this assistant. Not financial advice: verify everything and place "
    "any order yourself in the Angel One app."
)


def _client_or_error(session_id: str):
    return readonly_client(session_id)


def _as_float(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _not_logged_in() -> dict[str, Any]:
    return {"error": "No Angel One session. Log in first."}


def make_trading_tools(session_id: str) -> list[Callable[..., Any]]:
    """Build read-only trading-analysis tool callables bound to the given Angel session id."""

    def trading_get_risk_profile() -> dict[str, Any]:
        """Get the client's stored risk profile (age, goal, tolerance, limits)."""
        profile = risk_profiles.get(session_id)
        if profile is None:
            # Cache miss — try to load from DB using the logged-in Angel client_id
            client = _client_or_error(session_id)
            if client:
                try:
                    from db import get_session
                    from db.models import User
                    from sqlmodel import select
                    with get_session() as db:
                        user = db.exec(
                            select(User).where(User.angel_client_id == client.client_id)
                        ).first()
                    if user:
                        profile = load_profile(user.id)
                        if profile:
                            risk_profiles.set(session_id, profile)
                except Exception:
                    pass
        if profile is None:
            return {"error": "No risk profile set. Ask the user to fill in their risk profile first."}
        return profile.to_dict()

    def trading_get_ltp(exchange: str, tradingsymbol: str, symboltoken: str) -> dict[str, Any]:
        """Last traded price for an instrument."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_ltp(exchange, tradingsymbol, symboltoken)

    def trading_get_market_depth(symbol: str, exchange: str = "NSE") -> dict[str, Any]:
        """Full market depth (bids/asks) and quote for a symbol."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return fetch_market_depth(client, symbol, exchange)

    def trading_get_positions() -> dict[str, Any]:
        """Open intraday/delivery positions and day P&L."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_positions()

    def trading_get_holdings() -> dict[str, Any]:
        """List demat holdings with quantity, average price, LTP, P&L."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_holdings()

    def trading_get_funds() -> dict[str, Any]:
        """Available cash / margin snapshot."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.get_funds()

    def trading_get_stock_history(
        symbol: str,
        days: int = 30,
        interval: str = "ONE_DAY",
    ) -> dict[str, Any]:
        """Historical OHLCV candles for a symbol."""
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
        return out

    def trading_get_technicals(symbol: str, days: int = 120) -> dict[str, Any]:
        """RSI, MACD, MAs, support/resistance from daily candles."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        days = max(30, min(int(days), 365))
        raw = fetch_stock_history_candles(client, symbol, days, "ONE_DAY")
        if not raw.get("ok"):
            return raw
        candles = raw.get("candles") or []
        sym = raw.get("tradingsymbol", symbol)
        avg_price = None
        try:
            h_data = client.get_holdings()
            if h_data.get("status") and h_data.get("data"):
                for h in h_data["data"]:
                    if h.get("tradingsymbol") == sym:
                        avg_price = float(h.get("averageprice", 0) or 0)
                        break
        except Exception:
            pass
        return compute_technical_indicators(candles, sym, avg_price)

    def trading_search_scrip(exchange: str, search_text: str) -> dict[str, Any]:
        """Search instruments on an exchange to resolve symbol tokens."""
        client = _client_or_error(session_id)
        if not client:
            return _not_logged_in()
        return client.search_scrip(exchange, search_text)

    def trading_hypothetical_trade_idea(
        tradingsymbol: str,
        transactiontype: str,
        quantity: str,
        price: str = "0",
        producttype: str = "DELIVERY",
        exchange: str = "NSE",
        rationale: str = "",
    ) -> dict[str, Any]:
        """
        Describe a HYPOTHETICAL trade idea for discussion. Never executes, never stores.

        Returns the idea with an estimated value and how it compares with the
        client's risk profile. Always present the result to the user labelled
        "Hypothetical — not executed"; the user acts in the Angel One app if at all.
        """
        side = (transactiontype or "").strip().upper()
        if side not in ("BUY", "SELL"):
            return {"error": "transactiontype must be BUY or SELL"}
        qty = _as_float(quantity)
        px = _as_float(price)
        if qty <= 0:
            return {"error": "quantity must be a positive number"}

        params = {
            "tradingsymbol": tradingsymbol,
            "transactiontype": side,
            "quantity": quantity,
            "price": price,
            "producttype": (producttype or "").upper(),
            "exchange": exchange,
        }
        price_display = f"{px:g}" if px > 0 else "market"
        findings: list[str] = []
        profile = risk_profiles.get(session_id)
        if profile is None:
            findings.append("No risk profile set; suitability was not checked.")
        else:
            try:
                validate_against_profile(params, profile)
                findings.append("Within the stated risk-profile limits.")
            except ValueError as e:
                findings.append(f"Would breach the risk profile: {e}")

        return {
            "hypothetical": True,
            "executable": False,
            "label": HYPOTHETICAL_LABEL,
            "summary": f"{side} {qty:g} {tradingsymbol} @ {price_display} "
                       f"{params['producttype']} ({exchange})",
            "estimated_value": round(qty * px, 2) if px > 0 else None,
            "rationale": rationale,
            "risk_profile_findings": findings,
            "disclaimer": HYPOTHETICAL_DISCLAIMER,
        }

    def trading_list_pending_proposals() -> dict[str, Any]:
        """List historical trade proposals for this session (read-only; none can be executed)."""
        proposals = proposal_store.list_for_session(session_id)
        return {
            "proposals": [p.to_dict() for p in proposals],
            "count": len(proposals),
        }

    return [
        trading_get_risk_profile,
        trading_get_ltp,
        trading_get_market_depth,
        trading_get_positions,
        trading_get_holdings,
        trading_get_funds,
        trading_get_stock_history,
        trading_get_technicals,
        trading_search_scrip,
        trading_hypothetical_trade_idea,
        trading_list_pending_proposals,
    ]
