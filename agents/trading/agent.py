"""
Trading-analysis ``LlmAgent``: risk-aware, read-only, hypothetical ideas only.

The agent analyses prices, technicals and the client's risk profile and may
describe hypothetical trade ideas. It cannot place, modify, cancel or queue
orders: no tool for that exists, and execution has been removed from the app.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

_rr = str(Path(__file__).resolve().parents[2])
if _rr not in sys.path:
    sys.path.insert(0, _rr)

from agents.factory import build_llm_agent

if TYPE_CHECKING:
    from google.adk.agents import LlmAgent

from agents.trading.tools import make_trading_tools
from session_manager import sessions

logger = logging.getLogger(__name__)

TRADING_AGENT_APP_NAME = "my_finance_mcp_trading"

TRADING_INSTRUCTION = """\
You are a **read-only trading-analysis assistant** for Indian equities on Angel One (NSE/BSE).

CRITICAL RULES — read carefully:

1. You **cannot** place, modify, cancel, approve or queue orders, and no tool for it exists. If the user asks \
you to trade, say plainly that this assistant is read-only and that they can act in the Angel One app.
2. You may describe **hypothetical** trade ideas with ``trading_hypothetical_trade_idea``. Present every one \
labelled **"Hypothetical — not executed"**. Never call it an order, a proposal awaiting approval, or something \
you did. There is nothing to approve: typing APPROVE does nothing.
3. Call ``trading_get_risk_profile`` at the start of any trading conversation. If no profile is set, \
say that suitability cannot be checked and suggest filling in the risk profile on the trading page.
4. **Respect risk limits** when framing ideas:
   - Flag any idea whose value exceeds ``max_single_order_value``.
   - Flag any idea that would push a single stock above ``max_position_pct`` of the portfolio.
   - Flag product types not listed in ``allowed_products``.
5. Before describing an idea, always gather data:
   - Use ``trading_get_ltp`` or ``trading_get_market_depth`` for current prices.
   - Use ``trading_get_technicals`` for RSI, MACD, moving averages, support/resistance.
   - Use ``trading_get_holdings`` and ``trading_get_positions`` to understand existing exposure.
   - Use ``trading_get_funds`` to check available margin.
6. Use ``trading_search_scrip`` to resolve symbol tokens when needed.
7. ``trading_list_pending_proposals`` shows historical proposals only; none can be executed.
8. Explain your reasoning: why an idea might or might not suit the client's risk profile \
and current market data. Be concise but transparent, and keep analysis separate from facts about past trades.
9. If a tool returns an "error" field, explain it briefly and suggest next steps.

Disclaimer: Not financial advice. Hypothetical ideas are for analysis only; the user decides and acts in the Angel One app."""

_DEFAULT_ADK_ANGEL_SESSION_ID = "adk-trading-default"


def _bootstrap_angel_session_from_env(session_id: str) -> None:
    if sessions.get_client(session_id) is not None:
        return
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    api_key = (os.environ.get("ANGELONE_API_KEY") or os.environ.get("ANGEL_API_KEY") or "").strip()
    client_id = (os.environ.get("ANGELONE_CLIENT_ID") or os.environ.get("ANGEL_CLIENT_ID") or "").strip()
    password = (os.environ.get("ANGELONE_PASSWORD") or os.environ.get("ANGEL_PASSWORD") or "").strip()
    totp = (os.environ.get("ANGELONE_TOTP_SECRET") or os.environ.get("ANGEL_TOTP_SECRET") or "").strip()
    if not (api_key and client_id and password and totp):
        return
    try:
        sessions.create_session(session_id, api_key, client_id, password, totp)
        logger.info("Angel One session started for trading ADK (id prefix=%s)", session_id[:12])
    except Exception as e:
        logger.warning("Angel bootstrap from env failed: %s", e)


def build_trading_root_agent(
    angel_session_id: str | None = None,
    *,
    model: str | None = None,
    api_key: str | None = None,
    allow_env_bootstrap: bool = False,
) -> "LlmAgent":
    sid = (angel_session_id or "").strip()
    if allow_env_bootstrap:
        sid = sid or (os.environ.get("ADK_ANGEL_SESSION_ID") or "").strip() or _DEFAULT_ADK_ANGEL_SESSION_ID
        _bootstrap_angel_session_from_env(sid)
    elif not sid:
        raise ValueError("build_trading_root_agent needs the caller's own angel_session_id")
    tools = make_trading_tools(sid)
    return build_llm_agent(
        name="trading_agent",
        instruction=TRADING_INSTRUCTION,
        tools=tools,
        model=model,
        api_key=api_key,
        description="Read-only, risk-aware trading analysis with hypothetical ideas only.",
    )
