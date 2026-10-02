"""Per-stock fundamental and technical research endpoints."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse


from services.fundamental_service import get_stock_fundamentals
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
