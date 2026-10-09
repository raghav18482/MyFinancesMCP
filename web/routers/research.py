"""Per-stock fundamental and technical research endpoints."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse


from services.ai_service import DEFAULT_OPENROUTER_MODEL, summarize_fundamentals
from services.estimates_service import get_estimates
from services.fundamental_service import get_stock_fundamentals
from services.ownership_service import get_ownership
from services.peer_service import get_peer_comparison
from services.prediction_service import model_status
from services.technical_service import compute_technical_indicators
from services.market_data import (
    fetch_candles_safe,
    pick_scrip_row,
    scrip_search_key,
    search_scrip_cached,
)


from web.dependencies import (
    require_login,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Summaries are generated with the server's own OpenRouter key, so they are cached
# per (symbol, model) to bound what the deployment pays. The prompt holds only
# public company data — no holdings, no account — so a process-wide cache leaks
# nothing.
_summary_cache: dict[tuple[str, str], dict] = {}
_SUMMARY_TTL = 6 * 3600  # matches the fundamental data cache
# "Regenerate" skips the cache, but not within this long of the last generation:
# otherwise one user clicking it in a loop spends the deployment's credit.
_SUMMARY_REFRESH_COOLDOWN = 60


def _summary_model() -> str:
    """The model is the deployment's choice, never the caller's — it is our bill."""
    return os.environ.get("RESEARCH_SUMMARY_MODEL") or DEFAULT_OPENROUTER_MODEL


def _summary_cached(key: tuple[str, str]) -> str | None:
    entry = _summary_cache.get(key)
    if entry and (time.time() - entry["ts"]) < _SUMMARY_TTL:
        return entry["summary"]
    return None



# ── Research & Sector API endpoints ───────────────────────────────────────


@router.get("/api/research/fundamental")
async def api_research_fundamental(
    request: Request,
    symbol: str = Query(...),
):
    """Fetch fundamental analysis for a single stock."""
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        result = await asyncio.to_thread(get_stock_fundamentals, symbol)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Fundamental API error for %s", symbol)
        return JSONResponse({"error": str(e)}, status_code=500)



@router.post("/api/research/fundamental/summary")
async def api_research_fundamental_summary(request: Request):
    """Plain-English LLM summary of one stock's fundamentals, for beginners.

    The 0-100 score itself is computed in Python and already present on
    ``GET /api/research/fundamental`` — this only adds the prose explanation, so
    the page stays fully useful even when the summary is unavailable. Generated
    with the deployment's own ``OPENROUTER_API_KEY``; callers supply no key and
    cannot choose the model.
    """
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    symbol = (body.get("symbol") or "").strip()
    if not symbol:
        return JSONResponse({"error": "symbol is required"}, status_code=400)

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        logger.error("OPENROUTER_API_KEY is not set; cannot summarise %s", symbol)
        return JSONResponse(
            {"error": "AI summaries are not available right now."}, status_code=503
        )

    model = _summary_model()
    cache_key = (symbol, model)
    entry = _summary_cache.get(cache_key)
    fresh = _summary_cached(cache_key)
    # A refresh inside the cooldown is answered from the cache like any other call.
    refresh_allowed = entry is None or (time.time() - entry["ts"]) >= _SUMMARY_REFRESH_COOLDOWN

    if fresh and not (body.get("refresh") and refresh_allowed):
        return JSONResponse({"summary": fresh, "cached": True})

    try:
        fundamentals = await asyncio.to_thread(get_stock_fundamentals, symbol)
        if fundamentals.get("error"):
            return JSONResponse({"error": fundamentals["error"]}, status_code=400)

        summary = await summarize_fundamentals(api_key, fundamentals, model)
        _summary_cache[cache_key] = {"summary": summary, "ts": time.time()}
        return JSONResponse({"summary": summary, "cached": False})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("Fundamental summary error for %s", symbol)
        return JSONResponse({"error": f"Something went wrong: {e}"}, status_code=500)



@router.get("/api/research/technical")
async def api_research_technical(
    request: Request,
    symbol: str = Query(...),
    exchange: str = Query("NSE"),
    days: int = Query(365),
):
    """Fetch technical indicators for a single stock using Angel One candle data."""
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        sr = search_scrip_cached(client, exchange, scrip_search_key(symbol))
        row = pick_scrip_row(sr.get("data") if sr.get("status") else None, symbol)
        token = row.get("symboltoken") if row else None

        if not token:
            return JSONResponse({"error": f"Could not find token for {symbol}"}, status_code=404)

        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=min(days, 365))
        candles = fetch_candles_safe(
            client, exchange, token, "ONE_DAY",
            from_dt.strftime("%Y-%m-%d 09:15"),
            to_dt.strftime("%Y-%m-%d 15:30"),
        )

        if not candles:
            return JSONResponse({"error": f"No candle data for {symbol}"}, status_code=400)

        # Find avg buy price from holdings
        avg_price = None
        try:
            h_data = client.get_holdings()
            if h_data.get("status") and h_data.get("data"):
                for h in h_data["data"]:
                    if h.get("tradingsymbol") == symbol:
                        avg_price = float(h.get("averageprice", 0) or 0)
                        break
        except Exception:
            pass

        result = compute_technical_indicators(candles, symbol, avg_price)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Technical API error for %s", symbol)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/research/peers")
async def api_research_peers(request: Request, symbol: str = Query(...)):
    """Valuation and quality percentiles against the stock's NSE industry peers.

    Complements ``/api/research/fundamental``, which grades against absolute
    bands: that says whether a P/E of 28 is high, this says whether it is high
    *for this industry*.
    """
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        result = await asyncio.to_thread(get_peer_comparison, symbol)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Peer comparison error for %s", symbol)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/research/estimates")
async def api_research_estimates(request: Request, symbol: str = Query(...)):
    """Consensus estimates, revision momentum, surprise history and next result date."""
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        result = await asyncio.to_thread(get_estimates, symbol)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Estimates error for %s", symbol)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/research/ownership")
async def api_research_ownership(
    request: Request,
    symbol: str = Query(...),
    days: int = Query(90),
):
    """Delivery trend, bulk and block deals, and insider holding.

    The quarterly promoter/FII/DII split is reported as unknown rather than
    omitted: no free source supplies it for NSE tickers, and an absent section
    reads as "nothing to report" when it should read as "not known".
    """
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        result = await asyncio.to_thread(get_ownership, symbol, lookback_days=days)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Ownership error for %s", symbol)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/research/model-status")
async def api_research_model_status(request: Request):
    """Which prediction engine is live: a registered model, or the heuristic.

    Unauthenticated on purpose — it describes the server's own configuration
    and carries no user data, and the UI needs it to label predictions honestly
    before a user has logged in.
    """
    try:
        return JSONResponse(model_status())
    except Exception as e:
        logger.exception("Model status error")
        return JSONResponse({"error": str(e)}, status_code=500)
