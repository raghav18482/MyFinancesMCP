"""News retrieval and sentiment endpoints."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse


from services.news_service import (
    build_portfolio_sector_news,
    enrich_sectors_news_with_sentiment,
    normalize_period as normalize_news_period,
    search_news_articles,
)


from web.dependencies import (
    require_login,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── News API (gnews) ──────────────────────────────────────────────────────


@router.get("/api/news/portfolio")
async def api_news_portfolio(
    request: Request,
    period: str = Query("7d"),
):
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    period = normalize_news_period(period)
    try:
        result = await asyncio.to_thread(build_portfolio_sector_news, client, period)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Portfolio news API error")
        return JSONResponse({"error": str(e)}, status_code=500)



@router.get("/api/news/search")
async def api_news_search(
    request: Request,
    q: str = Query(""),
    period: str = Query("7d"),
    location: str = Query(""),
):
    if not q.strip():
        return JSONResponse({"error": "Search query is required"}, status_code=400)
    period = normalize_news_period(period)

    try:
        articles = await asyncio.to_thread(
            search_news_articles, q.strip(), period, location, 20
        )
        return JSONResponse({"articles": articles})
    except Exception as e:
        logger.exception("Search news API error")
        return JSONResponse({"error": str(e)}, status_code=500)



@router.post("/api/news/sentiment")
async def api_news_sentiment(request: Request):
    """
    Run FinBERT sentiment analysis on portfolio news.
    Body: {"sectors": [{"name": str, "invested": float, "news": [...]}]}
    Returns same structure with sentiment added per article and per sector.
    """
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    sectors = body.get("sectors", [])
    if not sectors:
        return JSONResponse({
            "sectors": [],
            "portfolio_sentiment": {
                "label": "neutral",
                "score": 0.0,
                "bullish": 0,
                "bearish": 0,
                "neutral": 0,
                "total_articles": 0,
            },
        })

    try:
        result = await asyncio.to_thread(enrich_sectors_news_with_sentiment, sectors)
        return JSONResponse(result)
    except ImportError as e:
        logger.warning("Sentiment analysis unavailable: %s", e)
        return JSONResponse(
            {"error": "Sentiment analysis requires transformers and torch. Install with: pip install transformers torch"},
            status_code=503,
        )
    except Exception as e:
        logger.exception("Sentiment API error")
        return JSONResponse({"error": str(e)}, status_code=500)
