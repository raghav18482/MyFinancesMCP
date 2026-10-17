"""
Fail-closed tool allowlist for every ADK agent.

The assistant is read-only. That is enforced here in code, not left to the
prompt: an agent can only be built with tools named in its allowlist, and every
tool call is checked again at execution time, so a tool name the model invents
— or one added to an agent by mistake later — is refused server-side.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# Names that must never be exposed to a model, whatever allowlist says.
FORBIDDEN_TOOL_RE = re.compile(
    r"place|modify|cancel|approve|execute|order_(create|update)|login|logout|"
    r"transfer|withdraw|deposit|smart_?api",
    re.IGNORECASE,
)

FINANCE_TOOLS: frozenset[str] = frozenset({
    "angel_get_profile",
    "angel_get_holdings",
    "angel_get_all_holdings",
    "angel_get_positions",
    "angel_get_order_book",
    "angel_get_trade_book",
    "angel_get_funds",
    "angel_get_ltp",
    "angel_search_scrip",
    "angel_portfolio_summary",
    "angel_holdings_for_sectors",
    "angel_get_candle_data",
    "angel_get_stock_history",
    "angel_calculate_pnl",
    "angel_get_market_depth",
    "research_stock_fundamentals",
    "research_market_breadth",
    "research_sector_portfolio_overview",
    "research_financial_text_sentiment",
    "research_technical_indicators",
    "research_price_prediction",
    "research_model_status",
    "research_portfolio_sector_news",
    "research_portfolio_news_with_sentiment",
    "research_news_search",
    "research_enrich_sector_news_with_sentiment",
})

TRADING_TOOLS: frozenset[str] = frozenset({
    "trading_get_risk_profile",
    "trading_get_ltp",
    "trading_get_market_depth",
    "trading_get_positions",
    "trading_get_holdings",
    "trading_get_funds",
    "trading_get_stock_history",
    "trading_get_technicals",
    "trading_search_scrip",
    "trading_hypothetical_trade_idea",
    "trading_list_pending_proposals",
})

ALLOWED_ADK_TOOLS: dict[str, frozenset[str]] = {
    "finance_agent": FINANCE_TOOLS,
    "trading_agent": TRADING_TOOLS,
}


class ToolNotPermitted(ValueError):
    """An agent was given a tool outside its read-only allowlist."""


def tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", "") or "")


def allowed_for(agent_name: str) -> frozenset[str]:
    """The allowlist for ``agent_name``; an unknown agent gets nothing."""
    return ALLOWED_ADK_TOOLS.get(agent_name, frozenset())


def is_permitted(agent_name: str, name: str) -> bool:
    return bool(name) and name in allowed_for(agent_name) and not FORBIDDEN_TOOL_RE.search(name)


def validate_tools(agent_name: str, tools: Iterable[Any]) -> None:
    """Raise :class:`ToolNotPermitted` unless every tool is allowlisted for the agent."""
    bad = [tool_name(t) or repr(t) for t in tools if not is_permitted(agent_name, tool_name(t))]
    if bad:
        raise ToolNotPermitted(
            f"agent {agent_name!r} may only use read-only allowlisted tools; refused: {sorted(bad)}"
        )


def make_before_tool_callback(agent_name: str) -> Callable[..., Optional[dict]]:
    """ADK ``before_tool_callback`` that refuses any call outside the allowlist.

    Returning a dict short-circuits the call: ADK reports it as the tool's
    response and the tool body never runs.
    """

    def before_tool_callback(tool: Any, args: dict[str, Any], tool_context: Any) -> Optional[dict]:
        name = tool_name(tool)
        if is_permitted(agent_name, name):
            return None
        logger.warning("Refused tool call %r for agent %s (not in read-only allowlist)",
                       name, agent_name)
        return {
            "error": f"Tool {name!r} is not permitted. This assistant is read-only and "
                     "cannot place, modify or cancel orders or change account settings.",
        }

    return before_tool_callback
