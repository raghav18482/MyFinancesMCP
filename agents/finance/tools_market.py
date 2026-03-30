"""
Research and market tools wrapping ``services.*`` (fundamentals, technicals, sectors, sentiment).

Some tools need a broker session (e.g. technicals use your average buy from holdings).
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from session_manager import sessions
from services.broker_service import fetch_stock_history_candles, holdings_for_sector_analysis
from services.fundamental_service import get_stock_fundamentals
from services.news_service import (
    build_portfolio_news_with_sentiment,
    build_portfolio_sector_news,
    enrich_sectors_news_with_sentiment,
    normalize_period as normalize_news_period,
    search_news_articles,
)
from services.sector_service import get_market_breadth, get_sector_overview
from services.sentiment_service import analyze_text
from services.technical_service import compute_technical_indicators

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sector_map() -> dict[str, str]:
    path = os.path.join(_repo_root, "data", "sector_map.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_market_tools(session_id: str) -> list[Callable[..., Any]]:
    """Tools that combine local analytics with optional broker-backed inputs."""

    def research_stock_fundamentals(trading_symbol: str) -> dict[str, Any]:
        """Fetch valuation, health, and trend data from yfinance for an NSE-style symbol."""
        return get_stock_fundamentals(trading_symbol)

    def research_market_breadth() -> dict[str, Any]:
        """NSE breadth: heatmap, 52w stats, VIX, advance/decline proxy."""
        return get_market_breadth()

    def research_sector_portfolio_overview() -> dict[str, Any]:
        """Sector weights and index performance for the logged-in user's holdings."""
        client = sessions.get_client(session_id)
        if client is None:
            return {"error": "No Angel session; cannot load holdings for sector overview."}
        rows = holdings_for_sector_analysis(client)
        if not rows:
            return {"error": "No holdings found for sector analysis."}
        return get_sector_overview(rows, _sector_map())

    def research_financial_text_sentiment(text: str) -> dict[str, Any]:
        """Run FinBERT-style sentiment on a short financial text (headline, snippet)."""
        return analyze_text(text)

    def research_technical_indicators(
        symbol: str,
        days: int = 120,
    ) -> dict[str, Any]:
        """RSI, MACD, MAs, support/resistance from daily candles; uses your avg buy if held."""
        client = sessions.get_client(session_id)
        if client is None:
            return {"error": "No Angel session; required to fetch price history."}
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

    def research_portfolio_sector_news(period: str = "7d") -> dict[str, Any]:
        """Fetch Google News headlines grouped by portfolio sector (top sectors by invested value)."""
        client = sessions.get_client(session_id)
        if client is None:
            return {"error": "No Angel session; required for holdings-based sector news."}
        p = normalize_news_period(period)
        return build_portfolio_sector_news(client, p)

    def research_portfolio_news_with_sentiment(period: str = "7d") -> dict[str, Any]:
        """Same as sector portfolio news plus FinBERT sentiment per article and sector aggregates."""
        client = sessions.get_client(session_id)
        if client is None:
            return {"error": "No Angel session; required for holdings-based news."}
        p = normalize_news_period(period)
        try:
            return build_portfolio_news_with_sentiment(client, p)
        except ImportError as e:
            return {"error": str(e), "hint": "Install transformers and torch for FinBERT sentiment."}

    def research_news_search(
        query: str,
        period: str = "7d",
        location: str = "",
    ) -> dict[str, Any]:
        """Search Google News (India). Optional location string appended to the query."""
        q = (query or "").strip()
        if not q:
            return {"error": "query is required", "articles": []}
        p = normalize_news_period(period)
        articles = search_news_articles(q, p, location, 20)
        return {"articles": articles, "count": len(articles)}

    def research_enrich_sector_news_with_sentiment(
        sectors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Run FinBERT on pre-fetched sector news bundles.
        Each sector dict should include: name, invested (optional), news (list of articles with title/description).
        Use after research_portfolio_sector_news or with your own article lists.
        """
        if not isinstance(sectors, list):
            return {"error": "sectors must be a list of objects", "sectors": []}
        try:
            return enrich_sectors_news_with_sentiment(sectors)
        except ImportError as e:
            return {"error": str(e), "hint": "Install transformers and torch for FinBERT sentiment."}

    return [
        research_stock_fundamentals,
        research_market_breadth,
        research_sector_portfolio_overview,
        research_financial_text_sentiment,
        research_technical_indicators,
        research_portfolio_sector_news,
        research_portfolio_news_with_sentiment,
        research_news_search,
        research_enrich_sector_news_with_sentiment,
    ]
