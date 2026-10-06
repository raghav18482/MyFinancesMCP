"""Portfolio analytics: holdings summary, candles, predictions and beta."""
from __future__ import annotations

import time
import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse


from services.prediction_service import predict_direction
from services.market_data import (
    fetch_candles_safe,
    pick_scrip_row,
    scrip_search_key,
    search_scrip_cached,
)
from services.portfolio_service import build_portfolio_data, compute_beta, get_portfolio_cached


from web.dependencies import (
    require_login,
    session_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()



# ── Portfolio Data API ─────────────────────────────────────────────────────


@router.get("/api/portfolio/analytics")
async def api_portfolio_analytics(request: Request):
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sid = session_id(request)
    return JSONResponse(get_portfolio_cached(sid, client))



@router.get("/api/portfolio/candles")
async def api_portfolio_candles(
    request: Request,
    symbol: str = Query(...),
    exchange: str = Query("NSE"),
    interval: str = Query("ONE_DAY"),
    days: int = Query(90),
):
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        sr = search_scrip_cached(client, exchange, scrip_search_key(symbol))
        row = pick_scrip_row(sr.get("data") if sr.get("status") else None, symbol)
        if not row or not row.get("symboltoken"):
            return JSONResponse({"error": f"Could not find token for {symbol}"}, status_code=404)
        token = row["symboltoken"]

        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=days)
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_dt.strftime("%Y-%m-%d 09:15"),
            "todate": to_dt.strftime("%Y-%m-%d 15:30"),
        }
        result = client.get_candle_data(params)
        if result.get("status") and result.get("data"):
            return JSONResponse({"candles": result["data"]})
        return JSONResponse({"error": result.get("message", "No candle data")}, status_code=400)
    except Exception as e:
        logger.exception("Candle data error")
        return JSONResponse({"error": str(e)}, status_code=500)



@router.get("/api/portfolio/predict")
async def api_portfolio_predict(
    request: Request,
    symbol: str = Query(...),
    exchange: str = Query("NSE"),
    days: int = Query(365),
):
    """Predict price direction for a stock across multiple timeframes (up to 1 year)."""
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        sr = search_scrip_cached(client, exchange, scrip_search_key(symbol))
        row = pick_scrip_row(sr.get("data") if sr.get("status") else None, symbol)
        if not row or not row.get("symboltoken"):
            return JSONResponse({"error": f"Could not find token for {symbol}"}, status_code=404)
        token = row["symboltoken"]

        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=days)
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": "ONE_DAY",
            "fromdate": from_dt.strftime("%Y-%m-%d 09:15"),
            "todate": to_dt.strftime("%Y-%m-%d 15:30"),
        }
        result = client.get_candle_data(params)
        if not result.get("status") or not result.get("data"):
            return JSONResponse({"error": "No candle data available"}, status_code=400)

        candles = result["data"]
        # The interval is passed explicitly and checked against the model's own
        # record: a model trained on different bars now raises instead of
        # silently reading the wrong distribution.
        prediction = await asyncio.to_thread(
            predict_direction, candles, symbol, interval="ONE_DAY"
        )
        return JSONResponse(prediction)
    except Exception as e:
        logger.exception("Prediction error for %s", symbol)
        return JSONResponse({"error": str(e)}, status_code=500)



@router.get("/api/portfolio/beta")
async def api_portfolio_beta(request: Request, days: int = Query(90)):
    """Compute portfolio beta vs NIFTY 50 using daily returns."""
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        portfolio = build_portfolio_data(client)
        holdings = portfolio.get("holdings", [])
        if not holdings:
            return JSONResponse({"error": "No holdings found"}, status_code=400)

        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=days)
        from_str = from_dt.strftime("%Y-%m-%d 09:15")
        to_str = to_dt.strftime("%Y-%m-%d 15:30")

        nifty_token = "99926000"
        nifty_candles = fetch_candles_safe(
            client, "NSE", nifty_token, "ONE_DAY", from_str, to_str
        )
        if not nifty_candles or len(nifty_candles) < 10:
            return JSONResponse({"error": "Could not fetch NIFTY 50 data"}, status_code=400)

        nifty_closes = {c[0].split("T")[0]: c[4] for c in nifty_candles}
        nifty_dates = sorted(nifty_closes.keys())
        nifty_returns = {}
        for i in range(1, len(nifty_dates)):
            prev = nifty_closes[nifty_dates[i - 1]]
            curr = nifty_closes[nifty_dates[i]]
            if prev > 0:
                nifty_returns[nifty_dates[i]] = (curr - prev) / prev

        sorted_holdings = sorted(holdings, key=lambda h: h["current"], reverse=True)
        top_holdings = sorted_holdings[:6]

        stock_daily = {}
        stock_betas = []
        total_weight = sum(h["current"] for h in top_holdings)

        for h in top_holdings:
            sym = h["symbol"]
            time.sleep(0.35)
            try:
                sr = search_scrip_cached(client, "NSE", scrip_search_key(sym))
                row = pick_scrip_row(sr.get("data") if sr and sr.get("status") else None, sym)
                if not row or not row.get("symboltoken"):
                    continue
                token = row["symboltoken"]

                time.sleep(0.35)
                candles = fetch_candles_safe(
                    client, "NSE", token, "ONE_DAY", from_str, to_str
                )
                if not candles or len(candles) < 10:
                    continue

                closes = {c[0].split("T")[0]: c[4] for c in candles}
                returns = {}
                dates = sorted(closes.keys())
                for i in range(1, len(dates)):
                    prev = closes[dates[i - 1]]
                    curr = closes[dates[i]]
                    if prev > 0:
                        returns[dates[i]] = (curr - prev) / prev

                weight = h["current"] / total_weight if total_weight else 0
                stock_daily[sym] = {"returns": returns, "weight": weight}

                common = set(returns.keys()) & set(nifty_returns.keys())
                if len(common) >= 10:
                    sb = compute_beta(
                        [nifty_returns[d] for d in sorted(common)],
                        [returns[d] for d in sorted(common)],
                    )
                    stock_betas.append({"symbol": sym, "beta": sb["beta"]})
            except Exception as e:
                logger.warning("Beta calc – skip %s: %s", sym, e)
                continue

        common_dates = set(nifty_returns.keys())
        for sd in stock_daily.values():
            common_dates &= set(sd["returns"].keys())
        common_dates = sorted(common_dates)

        if len(common_dates) < 10:
            return JSONResponse({"error": "Not enough overlapping trading days"}, status_code=400)

        port_returns = []
        nifty_ret_list = []
        for d in common_dates:
            pr = sum(
                sd["returns"].get(d, 0) * sd["weight"]
                for sd in stock_daily.values()
            )
            port_returns.append(pr)
            nifty_ret_list.append(nifty_returns[d])

        result = compute_beta(nifty_ret_list, port_returns)
        result["stock_betas"] = sorted(stock_betas, key=lambda x: x["beta"], reverse=True)
        result["days_used"] = len(common_dates)

        nifty_cum = []
        port_cum = []
        n_acc = 1.0
        p_acc = 1.0
        for i in range(len(common_dates)):
            n_acc *= (1 + nifty_ret_list[i])
            p_acc *= (1 + port_returns[i])
            nifty_cum.append(round((n_acc - 1) * 100, 4))
            port_cum.append(round((p_acc - 1) * 100, 4))

        result["dates"] = common_dates
        result["nifty_cumulative"] = nifty_cum
        result["portfolio_cumulative"] = port_cum

        return JSONResponse(result)

    except Exception as e:
        logger.exception("Beta computation error")
        return JSONResponse({"error": str(e)}, status_code=500)
