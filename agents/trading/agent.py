"""
Trading ``LlmAgent``: risk-aware, proposal-based order execution.

The agent analyses prices and technicals, then proposes trades. It never
places orders directly — execution requires explicit client approval.
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
You are a **trading assistant** for Indian equities on Angel One (NSE/BSE).

CRITICAL RULES — read carefully:

1. You **NEVER** execute orders directly. You only **propose** trades using ``trading_propose_order``.
2. After proposing, tell the user:
   - The **proposal ID** and a clear summary of the trade.
   - That they must click **Approve** in the trading panel OR type ``APPROVE <proposal_id>`` in this chat.
3. Call ``trading_get_risk_profile`` at the start of any trading conversation. If no profile is set, \
**ask the user to fill in their risk profile** on the trading page before you propose any trade.
4. **Respect risk limits**:
   - Do not propose an order whose value exceeds ``max_single_order_value``.
   - Do not propose a position that would push a single stock above ``max_position_pct`` of the portfolio.
   - Only use product types listed in ``allowed_products``.
5. Before proposing a trade, always gather data:
   - Use ``trading_get_ltp`` or ``trading_get_market_depth`` for current prices.
   - Use ``trading_get_technicals`` for RSI, MACD, moving averages, support/resistance.
   - Use ``trading_get_holdings`` and ``trading_get_positions`` to understand existing exposure.
   - Use ``trading_get_funds`` to check available margin.
6. Use ``trading_search_scrip`` to resolve symbol tokens when needed.
7. Use ``trading_list_pending_proposals`` to show the user their pending/past proposals.
8. Explain your reasoning: why you think a trade is suitable given the client's risk profile \
and current market data. Be concise but transparent.
9. If a tool returns an "error" field, explain it briefly and suggest next steps.

Disclaimer: Not financial advice. The user should verify all details in the Angel One app before approving."""

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
) -> "LlmAgent":
    sid = (angel_session_id or os.environ.get("ADK_ANGEL_SESSION_ID") or "").strip()
    if not sid:
        sid = _DEFAULT_ADK_ANGEL_SESSION_ID
    _bootstrap_angel_session_from_env(sid)
    tools = make_trading_tools(sid)
    return build_llm_agent(
        name="trading_agent",
        instruction=TRADING_INSTRUCTION,
        tools=tools,
        model=model,
        api_key=api_key,
        description="Risk-aware trading assistant with proposal-based execution.",
    )
