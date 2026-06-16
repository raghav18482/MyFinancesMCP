"""
Trading agent tools — proposal-based, no direct order execution.

Tools close over ``session_id`` for broker data access and proposal ownership.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from session_manager import sessions
from services.broker_service import (
    fetch_market_depth,
    fetch_stock_history_candles,
)
from services.risk_profile import risk_profiles, load_profile
from services.trade_proposals import proposal_store
from services.technical_service import compute_technical_indicators

logger = logging.getLogger(__name__)

_MAX_HISTORY_CANDLES = 40


def _client_or_error(session_id: str):
    return sessions.get_client(session_id)


def _not_logged_in() -> dict[str, Any]:
    return {"error": "No Angel One session. Log in first."}


def make_trading_tools(session_id: str) -> list[Callable[..., Any]]:
    """Build trading tool callables bound to the given Angel session id."""

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

    def trading_propose_order(
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
        """
        Propose a trade for client approval. Does NOT execute.
        Returns a proposal_id that the client must APPROVE or REJECT.
        """
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

        price_display = price if float(price) > 0 else "market"
        summary = (
            f"{transactiontype} {quantity} {tradingsymbol} @ {price_display} "
            f"{producttype} ({exchange})"
        )

        proposal = proposal_store.create(
            session_id=session_id,
            order_params=order_params,
            summary=summary,
        )

        logger.info("Proposal %s created: %s", proposal.proposal_id, summary)

        return {
            "ok": True,
            "proposal_id": proposal.proposal_id,
            "summary": summary,
            "status": "pending",
            "message": (
                f"Trade proposal created: {summary}. "
                f"Proposal ID: {proposal.proposal_id}. "
                f"The client must click Approve in the UI or type APPROVE {proposal.proposal_id} to execute."
            ),
        }

    def trading_list_pending_proposals() -> dict[str, Any]:
        """List all trade proposals for this session with their current status."""
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
        trading_propose_order,
        trading_list_pending_proposals,
    ]
