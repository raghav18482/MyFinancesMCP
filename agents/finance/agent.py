"""
Finance ``LlmAgent``: OpenRouter via LiteLLM, read-only Angel broker tools, research tools.

Multi-user callers (the web app's runner registry) must pass the caller's own
``angel_session_id``; an empty id is refused rather than falling back to a
shared one. The single-developer ``adk web`` / CLI flows opt in to
``allow_env_bootstrap=True``, which may build a session from ``.env``
credentials under ``ADK_ANGEL_SESSION_ID`` or a fixed default id.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# If this module is loaded without going through ``finance`` package ``__init__.py`` (unusual),
# still ensure project root is on path for ``adk web``.
_rr = str(Path(__file__).resolve().parents[2])
if _rr not in sys.path:
    sys.path.insert(0, _rr)

from agents.factory import build_llm_agent

if TYPE_CHECKING:
    from google.adk.agents import LlmAgent

from agents.finance.tools_broker import make_broker_tools
from agents.finance.tools_market import make_market_tools
from session_manager import sessions

logger = logging.getLogger(__name__)

FINANCE_AGENT_APP_NAME = "my_finance_mcp_finance"

FINANCE_INSTRUCTION = """You are a portfolio and Indian equities assistant (Angel One / NSE context).

Behavior:
- Call tools whenever the user needs live broker data, prices, or portfolio facts. Do not guess numbers.
- Broker tools are prefixed with angel_*. They are read-only and require an active Angel session (the host app
  creates it).
- You are a READ-ONLY assistant. You cannot place, modify or cancel orders, move money, or change account
  settings, and no tool exists for it. If asked, say so plainly and point the user to the Angel One app.
- You may discuss hypothetical scenarios and trade ideas. Label every one "Hypothetical — not executed" and never
  describe it as an order, a placed trade, or something you did. Past orders and trades come only from
  angel_get_order_book / angel_get_trade_book; describe those as historical facts.
- Research tools are prefixed with research_* (fundamentals via yfinance, NSE breadth, sector breakdown, FinBERT
  sentiment, technicals from candles, price prediction, Google News search, portfolio news by sector, optional
  news+sentiment bundle). News uses gnews and may rate-limit; sentiment on many articles is slower (FinBERT).
- If a tool returns an \"error\" field, explain it briefly and suggest next steps (e.g. log in, check symbol).

Predictions — read this before quoting any number from research_price_prediction:
- Quote ``probability_up`` as it is returned. Do not round it into a different claim, do not convert it into a
  price target, and never state a probability the tool did not give you.
- Check ``model_type``. If it is \"heuristic\" there is NO trained model: the numbers are rule-based technical
  scores. Say that plainly — \"a technical score, not a model prediction\" — and do not call it ML or AI.
- If ``calibrated`` is false, the confidence is not a real probability. Say so rather than presenting it as one.
- A probability near 0.5 means the model has no view. Report that as no edge; do not dress it up as a signal.
- These are direction probabilities over a horizon, not forecasts of a price, and not advice.

Disclaimer: Not financial advice. User should verify data in the Angel One app."""

# Single-developer default for ``adk web`` / CLI only (``allow_env_bootstrap=True``).
_DEFAULT_ADK_ANGEL_SESSION_ID = "adk-finance-default"


def _bootstrap_angel_session_from_env(session_id: str) -> None:
    """Create Angel session in this process if env has creds and session is missing."""
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
        logger.info("Angel One session started for ADK (id prefix=%s)", session_id[:12])
    except Exception as e:
        logger.warning("Angel bootstrap from env failed: %s", e)


def build_finance_root_agent(
    angel_session_id: str | None = None,
    *,
    model: str | None = None,
    api_key: str | None = None,
    allow_env_bootstrap: bool = False,
) -> LlmAgent:
    sid = (angel_session_id or "").strip()
    if allow_env_bootstrap:
        sid = sid or (os.environ.get("ADK_ANGEL_SESSION_ID") or "").strip() or _DEFAULT_ADK_ANGEL_SESSION_ID
        _bootstrap_angel_session_from_env(sid)
    elif not sid:
        raise ValueError("build_finance_root_agent needs the caller's own angel_session_id")
    tools = [*make_broker_tools(sid), *make_market_tools(sid)]
    return build_llm_agent(
        name="finance_agent",
        instruction=FINANCE_INSTRUCTION,
        tools=tools,
        model=model,
        api_key=api_key,
        description="Read-only Angel One portfolio analysis and India market research.",
    )


_root_agent_cache = None


def __getattr__(name: str):
    """Lazy ``root_agent`` so importing the module does not require OPENROUTER_API_KEY."""
    global _root_agent_cache
    if name == "root_agent":
        if _root_agent_cache is None:
            _root_agent_cache = build_finance_root_agent(allow_env_bootstrap=True)
        return _root_agent_cache
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
