"""
LangGraph tools: broker (in-process, MCP-parity), RAG, web search, long-term memory.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool

from agent_langgraph.context import get_angel_client, get_user_key
from agent_langgraph.user_memory_store import get_memory_store
from agent_langgraph.web_search import search_web
from portfolio_snapshot import build_portfolio_data
from rag.chroma_store import ChromaRAG, multi_collection_query

logger = logging.getLogger(__name__)

_rag_singleton: ChromaRAG | None = None


def _get_rag() -> ChromaRAG:
    global _rag_singleton
    if _rag_singleton is None:
        _rag_singleton = ChromaRAG()
    return _rag_singleton


@tool
def get_portfolio_snapshot() -> str:
    """Get current user's portfolio as JSON: holdings, summary P&L, funds, day P&L. Always use for live positions."""
    client = get_angel_client()
    client.ensure_session()
    return json.dumps(build_portfolio_data(client), indent=2, default=str)


@tool
def get_ltp_for_symbol(symbol: str, exchange: str = "NSE") -> str:
    """Resolve a stock name/symbol on the exchange and return last traded price JSON."""
    client = get_angel_client()
    client.ensure_session()
    sym = symbol.strip().upper()
    scrip = client.search_scrip(exchange, sym.replace("-EQ", ""))
    if not scrip.get("status") or not scrip.get("data"):
        return json.dumps({"error": f"No match for {symbol} on {exchange}"})
    match = scrip["data"][0]
    token = match["symboltoken"]
    tsym = match["tradingsymbol"]
    ex = match.get("exchange", exchange)
    ltp = client.get_ltp(ex, tsym, token)
    return json.dumps({"tradingsymbol": tsym, "exchange": ex, "ltp_response": ltp}, default=str)


@tool
def get_funds_snapshot() -> str:
    """Available cash, margin, and net balance from the broker."""
    client = get_angel_client()
    client.ensure_session()
    return json.dumps(client.get_funds(), default=str)


@tool
def web_search_news(query: str) -> str:
    """Search the web for recent news or macro context (headlines/snippets). Not a substitute for live prices."""
    return search_web(query, max_results=5)


@tool
def rag_search_financial_knowledge(query: str) -> str:
    """Search curated knowledge: sector drivers, product metrics, market basics. Does not contain live portfolio data."""
    try:
        rag = _get_rag()
        return multi_collection_query(
            rag,
            query,
            collections=["sector_knowledge", "product_help", "market_reference", "user_rules"],
            k_per_collection=3,
        )
    except Exception as e:
        logger.warning("RAG search failed: %s", e)
        return f"RAG unavailable or empty: {e}. Run: python -m rag.ingest --rebuild"


@tool
def memory_get(key: str) -> str:
    """Read a long-term preference value for this user. Keys e.g. risk_appetite, goals, sectors_avoid."""
    store = get_memory_store()
    uk = get_user_key()
    val = store.get(uk, key.strip())
    if val is None:
        return json.dumps({"key": key, "value": None})
    return json.dumps({"key": key, "value": val})


@tool
def memory_set(key: str, value: str) -> str:
    """Store a long-term preference (string or JSON string). Use for risk profile, goals, sector preferences."""
    store = get_memory_store()
    uk = get_user_key()
    k = key.strip()
    parsed: Any = value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        pass
    store.set(uk, k, parsed)
    return json.dumps({"ok": True, "key": k})


def all_tools():
    return [
        get_portfolio_snapshot,
        get_ltp_for_symbol,
        get_funds_snapshot,
        web_search_news,
        rag_search_financial_knowledge,
        memory_get,
        memory_set,
    ]
