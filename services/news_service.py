"""
Google News (gnews) helpers and portfolio-sector news grouping.

Shared by the web dashboard and ADK research tools.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from gnews import GNews

from services.sentiment_service import analyze_articles, compute_sector_sentiment

logger = logging.getLogger(__name__)

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VALID_NEWS_PERIODS = frozenset({"1d", "7d", "1m", "3m", "6m", "1y"})
_MAX_SECTORS = 8

SECTOR_QUERIES: dict[str, str] = {
    "Banking": "Banking sector India stock market",
    "IT": "IT sector India Infosys TCS Wipro stock",
    "Energy": "Energy oil gas power India stock market",
    "Pharma": "Pharma sector India drug stock market",
    "Healthcare": "Healthcare hospital India stock market",
    "Financial Services": "NBFC insurance mutual fund India stock",
    "FMCG": "FMCG consumer goods India stock market",
    "Automobile": "Automobile auto EV India stock market",
    "Metals": "Metals steel copper India stock market",
    "Infrastructure": "Infrastructure cement construction India stock",
    "Real Estate": "Real estate realty India stock market",
    "Consumer Durables": "Consumer durables electronics India stock",
    "Chemicals": "Chemical sector India stock market",
    "Digital / New Age": "Startup fintech e-commerce India stock",
    "Telecom": "Telecom 5G spectrum India stock market",
    "Travel & Tourism": "Travel tourism airline India stock",
    "Defence": "Defence defense India stock market",
    "PSU": "PSU public sector India stock market",
    "ETF": "ETF index fund India stock market",
    "ETF - Gold": "Gold ETF India market price",
    "ETF - Silver": "Silver ETF India market price",
    "ETF - CPSE": "CPSE ETF India PSU disinvestment",
    "ETF - Midcap": "Midcap ETF India stock market",
    "ETF - Smallcap": "Smallcap ETF India stock market",
    "ETF - Nifty Next 50": "Nifty Next 50 ETF India market",
    "ETF - PSU Bank": "PSU bank India stock market",
    "ETF - Metals": "Metal ETF India stock market",
    "ETF - Pharma": "Pharma ETF India stock market",
    "ETF - Infra": "Infrastructure ETF India stock market",
    "ETF - Global Tech": "Global tech fund India NASDAQ",
}

_sector_map_cache: dict[str, str] | None = None


def get_sector_map() -> dict[str, str]:
    global _sector_map_cache
    if _sector_map_cache is None:
        import os

        path = os.path.join(_repo_root, "data", "sector_map.json")
        with open(path, encoding="utf-8") as f:
            _sector_map_cache = json.load(f)
    return _sector_map_cache


def normalize_period(period: str, default: str = "7d") -> str:
    p = (period or default).strip().lower()
    return p if p in VALID_NEWS_PERIODS else default


def gnews_article_to_dict(article: dict) -> dict[str, Any]:
    publisher = article.get("publisher") or {}
    return {
        "title": article.get("title", ""),
        "link": article.get("url", "#"),
        "date": article.get("published date", ""),
        "description": article.get("description", ""),
        "source": publisher.get("title", "") if isinstance(publisher, dict) else str(publisher),
    }


def fetch_sector_news(query: str, period: str, max_results: int) -> list[dict[str, Any]]:
    try:
        gn = GNews(language="en", country="IN", period=period, max_results=max_results)
        raw = gn.get_news(query)
        return [gnews_article_to_dict(a) for a in (raw or [])]
    except Exception as e:
        logger.warning("gnews query failed for %r: %s", query, e)
        return []


def search_news_articles(
    query: str,
    period: str = "7d",
    location: str = "",
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """Ad hoc Google News search (India-focused)."""
    q = (query or "").strip()
    if not q:
        return []
    period = normalize_period(period)
    try:
        gn = GNews(language="en", country="IN", period=period, max_results=max_results)
        loc = (location or "").strip()
        raw = gn.get_news(f"{q} {loc}") if loc else gn.get_news(q)
        return [gnews_article_to_dict(a) for a in (raw or [])]
    except Exception as e:
        logger.warning("gnews search failed for %r: %s", q, e)
        return []


def build_portfolio_sector_news(client: Any, period: str = "7d") -> dict[str, Any]:
    """
    Group holdings by sector (using sector_map), fetch a few headlines per top sector.
    Returns {"sectors": [{"name", "invested", "news"}, ...]}.
    """
    period = normalize_period(period)
    sector_map = get_sector_map()
    sector_invested: dict[str, float] = {}

    try:
        h_data = client.get_holdings()
        if h_data.get("status") and h_data.get("data"):
            for h in h_data["data"]:
                sym = h.get("tradingsymbol", "")
                qty = int(h.get("quantity", 0) or 0)
                avg = float(h.get("averageprice", 0) or 0)
                invested = qty * avg
                sector = sector_map.get(sym, "Other")
                sector_invested[sector] = sector_invested.get(sector, 0) + invested
    except Exception as e:
        logger.warning("News – holdings fetch error: %s", e)

    sector_order = sorted(
        sector_invested.keys(), key=lambda s: sector_invested[s], reverse=True
    )[:_MAX_SECTORS]

    results: dict[str, list[dict[str, Any]]] = {}
    for sector in sector_order:
        q = SECTOR_QUERIES.get(sector, f"{sector} India stock market")
        results[sector] = fetch_sector_news(q, period, 5)
        time.sleep(0.5)

    sectors_list = []
    for sector in sector_order:
        sectors_list.append(
            {
                "name": sector,
                "invested": round(sector_invested.get(sector, 0), 2),
                "news": results.get(sector, []),
            }
        )

    return {"sectors": sectors_list}


def enrich_sectors_news_with_sentiment(
    sectors: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Run FinBERT on each sector's articles; add sentiment per article and sector aggregates.
    Input shape matches build_portfolio_sector_news output sectors list.
    """
    if not sectors:
        return {
            "sectors": [],
            "portfolio_sentiment": {
                "label": "neutral",
                "score": 0.0,
                "bullish": 0,
                "bearish": 0,
                "neutral": 0,
                "total_articles": 0,
            },
        }

    enriched_sectors = []
    overall_bullish = 0
    overall_bearish = 0
    overall_neutral = 0

    for sector in sectors:
        articles = sector.get("news", [])
        analyzed = analyze_articles(articles)
        sector_agg = compute_sector_sentiment(analyzed)

        enriched_sectors.append(
            {
                "name": sector.get("name", ""),
                "invested": sector.get("invested", 0),
                "news": analyzed,
                "sentiment_summary": sector_agg,
            }
        )
        overall_bullish += sector_agg["bullish"]
        overall_bearish += sector_agg["bearish"]
        overall_neutral += sector_agg["neutral"]

    total = overall_bullish + overall_bearish + overall_neutral
    overall_score = (overall_bullish - overall_bearish) / total if total else 0.0
    if overall_score > 0.2:
        overall_label = "bullish"
    elif overall_score < -0.2:
        overall_label = "bearish"
    else:
        overall_label = "neutral"

    return {
        "sectors": enriched_sectors,
        "portfolio_sentiment": {
            "label": overall_label,
            "score": round(overall_score, 3),
            "bullish": overall_bullish,
            "bearish": overall_bearish,
            "neutral": overall_neutral,
            "total_articles": total,
        },
    }


def build_portfolio_news_with_sentiment(client: Any, period: str = "7d") -> dict[str, Any]:
    """Portfolio sector news plus FinBERT in one call."""
    base = build_portfolio_sector_news(client, period)
    return enrich_sectors_news_with_sentiment(base.get("sectors", []))
