"""Sector allocation and market breadth endpoints."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


from services.news_service import (
    get_sector_map,
)
from services.sector_service import get_sector_overview, get_market_breadth
from services.portfolio_service import build_portfolio_data


from web.dependencies import (
    require_login,
)

logger = logging.getLogger(__name__)

router = APIRouter()



@router.get("/api/sectors/overview")
async def api_sectors_overview(request: Request):
    """Get sector-level analysis for the user's portfolio."""
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        portfolio = build_portfolio_data(client)
        holdings = portfolio.get("holdings", [])
        if not holdings:
            return JSONResponse({"error": "No holdings found"}, status_code=400)

        result = await asyncio.to_thread(get_sector_overview, holdings, get_sector_map())
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Sectors overview API error")
        return JSONResponse({"error": str(e)}, status_code=500)



@router.get("/api/sectors/breadth")
async def api_sectors_breadth(request: Request):
    """Get market breadth indicators."""
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        result = await asyncio.to_thread(get_market_breadth)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Market breadth API error")
        return JSONResponse({"error": str(e)}, status_code=500)
