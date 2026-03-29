"""
Finance ``LlmAgent``: OpenRouter via LiteLLM, Angel broker tools, research tools.

Set ``ADK_ANGEL_SESSION_ID`` to the same id passed to ``session_manager.sessions.create_session``
so broker tools resolve your Angel One client. The CLI runner sets this automatically.

For ``adk web``: load ``.env`` from the repo with Angel + OpenRouter vars; a shared in-process
session is created from env on first agent build if credentials are present (see below).
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
- Broker tools are prefixed with angel_*. They require an active in-memory Angel session (the host app creates it).
- Order tools (angel_place_order, angel_modify_order, angel_cancel_order) move real money. Repeat the full order
  details back to the user and obtain an explicit yes before you call them.
- Research tools are prefixed with research_* (fundamentals via yfinance, NSE breadth, sector breakdown, FinBERT
  sentiment, technicals from candles). These can be delayed vs live quotes.
- If a tool returns an \"error\" field, explain it briefly and suggest next steps (e.g. log in, check symbol).

Disclaimer: Not financial advice. User should verify data in the Angel One app."""

# Stable default when ``ADK_ANGEL_SESSION_ID`` is unset (``adk web`` + .env bootstrap).
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
) -> LlmAgent:
    sid = (angel_session_id or os.environ.get("ADK_ANGEL_SESSION_ID") or "").strip()
    if not sid:
        sid = _DEFAULT_ADK_ANGEL_SESSION_ID
    os.environ.setdefault("ADK_ANGEL_SESSION_ID", sid)
    _bootstrap_angel_session_from_env(sid)
    tools = [*make_broker_tools(sid), *make_market_tools(sid)]
    return build_llm_agent(
        name="finance_agent",
        instruction=FINANCE_INSTRUCTION,
        tools=tools,
        model=model,
        api_key=api_key,
        description="Angel One portfolio, trading, and India market research.",
    )


_root_agent_cache = None


def __getattr__(name: str):
    """Lazy ``root_agent`` so importing the module does not require OPENROUTER_API_KEY."""
    global _root_agent_cache
    if name == "root_agent":
        if _root_agent_cache is None:
            _root_agent_cache = build_finance_root_agent()
        return _root_agent_cache
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
