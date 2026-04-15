import os
import json
import re
import uuid
import time
import asyncio
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass

from fastapi import FastAPI, Request, Form, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from services.ai_service import DEFAULT_OPENROUTER_MODEL, ask_question, generate_insights
from services.fundamental_service import get_stock_fundamentals
from services.news_service import (
    build_portfolio_sector_news,
    enrich_sectors_news_with_sentiment,
    get_sector_map,
    normalize_period as normalize_news_period,
    search_news_articles,
)
from services.technical_service import compute_technical_indicators
from services.prediction_service import predict_direction
from services.sector_service import get_sector_overview, get_market_breadth
from services.risk_profile import risk_profiles, build_profile_from_dict
from services.trade_proposals import proposal_store, execute_proposal
from services.realtime_feed import feed_relay, poll_ltp_fallback

from session_manager import sessions
from services.adk_runner_registry import registry

logger = logging.getLogger(__name__)

# Starlette session keys: separate ADK chat ids for finance vs trading agent.
_ADK_CHAT_SESSION_KEY = "adk_chat_session_id"
_ADK_TRADING_CHAT_SESSION_KEY = "adk_trading_chat_session_id"

_APPROVE_RE = re.compile(r"^APPROVE\s+([a-f0-9]{8,16})$", re.IGNORECASE)
_REJECT_RE = re.compile(r"^REJECT\s+([a-f0-9]{8,16})$", re.IGNORECASE)

_dir = os.path.dirname(os.path.abspath(__file__))
_frontend_dir = os.path.join(_dir, "frontend")

web = FastAPI(docs_url=None, redoc_url=None)
web.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", uuid.uuid4().hex),
)
web.mount("/static/data", StaticFiles(directory=os.path.join(_dir, "data")), name="data")
web.mount("/static", StaticFiles(directory=os.path.join(_frontend_dir, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_frontend_dir, "templates"))

SECTOR_MAP: dict[str, str] = get_sector_map()


def _sid(request: Request) -> str | None:
    return request.session.get("sid")


def _ensure_adk_chat_session_id(request: Request, key: str = _ADK_CHAT_SESSION_KEY) -> str:
    raw = request.session.get(key)
    if not raw:
        raw = uuid.uuid4().hex
        request.session[key] = raw
    return str(raw)


def _ctx(request: Request, active: str = "") -> dict:
    return {
        "request": request,
        "logged_in": _sid(request) is not None and sessions.get_client(_sid(request)) is not None,
        "active": active,
    }


# ── Public routes ──────────────────────────────────────────────────────────


@web.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    if _sid(request) and sessions.get_client(_sid(request)):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "landing.html", _ctx(request))


@web.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return templates.TemplateResponse(request, "setup.html", _ctx(request, "setup"))


@web.get("/connect", response_class=HTMLResponse)
async def connect_page(request: Request):
    server_url = str(request.base_url).rstrip("/")
    ctx = _ctx(request, "connect")
    ctx["server_url"] = server_url
    return templates.TemplateResponse(request, "connect.html", ctx)


@web.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _sid(request) and sessions.get_client(_sid(request)):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "login.html", _ctx(request))


@web.post("/login")
async def login_submit(
    request: Request,
    api_key: str = Form(...),
    client_id: str = Form(...),
    password: str = Form(...),
    totp_secret: str = Form(...),
):
    try:
        sid = uuid.uuid4().hex
        sessions.create_session(sid, api_key, client_id, password, totp_secret)
        request.session["sid"] = sid
        return RedirectResponse("/dashboard", status_code=302)
    except Exception as e:
        logger.exception("Web login failed")
        ctx = _ctx(request)
        ctx["error"] = f"Login failed: {e}"
        return templates.TemplateResponse(request, "login.html", ctx)


@web.post("/logout")
async def logout(request: Request):
    sid = _sid(request)
    if sid:
        sessions.remove_session(sid)
    request.session.clear()
    return RedirectResponse("/", status_code=302)


# ── Authenticated routes ───────────────────────────────────────────────────


def _require_login(request: Request):
    sid = _sid(request)
    if not sid:
        return None
    return sessions.get_client(sid)


def _scrip_search_key(tradingsymbol: str) -> str:
    """Root symbol for Angel searchScrip (e.g. GROWW from GROWW-BE, RELIANCE from RELIANCE-EQ)."""
    sym = (tradingsymbol or "").strip().upper()
    if "-" in sym:
        return sym.rsplit("-", 1)[0]
    return sym


def _pick_scrip_row(data: list | None, requested_tradingsymbol: str) -> dict | None:
    """
    Pick the row whose tradingsymbol matches the chart/holding symbol.

    Angel returns multiple series (EQ, BE, BL, …); using data[0] often pairs the
    wrong symboltoken with the requested name and triggers AB4006 Invalid symboltoken.
    """
    if not data:
        return None
    req = (requested_tradingsymbol or "").strip()
    req_u = req.upper()
    for item in data:
        ts = item.get("tradingsymbol") or ""
        if ts == req or ts.upper() == req_u:
            return item
    base = _scrip_search_key(req)
    if base and base.upper() != req_u:
        eq_sym = f"{base}-EQ"
        for item in data:
            ts = (item.get("tradingsymbol") or "").upper()
            if ts == eq_sym.upper():
                return item
    return data[0]


_search_scrip_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_SCRIP_SEARCH_CACHE_TTL = 300.0  # seconds — cuts Angel rate limits across WS / candles / research


def _search_scrip_cached(client, exchange: str, search_key: str) -> dict:
    """
    Cached ``searchScrip`` with one retry on rate-limit text from Angel.

    Many UI actions (candles + live WS + analytics) resolve the same symbol; caching
    avoids duplicate broker calls. Retry backs off briefly when Angel returns plain-text
    ``Access denied because of exceeding access rate`` (SmartApi raises DataException).
    """
    key = (exchange.upper(), search_key.upper())
    now = time.time()
    hit = _search_scrip_cache.get(key)
    if hit and (now - hit[0]) < _SCRIP_SEARCH_CACHE_TTL:
        return hit[1]
    for attempt in range(2):
        if attempt:
            time.sleep(2.5)
        try:
            out = client.search_scrip(exchange, search_key)
            _search_scrip_cache[key] = (time.time(), out)
            return out
        except Exception as e:
            low = str(e).lower()
            if attempt == 0 and ("exceeding access rate" in low or "access denied" in low):
                continue
            raise
    raise RuntimeError("search_scrip failed after retry")  # pragma: no cover


@web.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)

    ctx = _ctx(request, "dashboard")
    try:
        total_invested = 0.0
        current_value = 0.0
        total_pnl = 0.0
        total_pnl_pct = 0.0
        day_pnl = 0.0
        available_cash = "N/A"
        net_value = "N/A"
        holdings_list = []

        try:
            h_data = client.get_holdings()
            if h_data.get("status") and h_data.get("data"):
                for h in h_data["data"]:
                    row = _HoldingRow(
                        symbol=h.get("tradingsymbol", "N/A"),
                        qty=int(h.get("quantity", 0) or 0),
                        avg_price=float(h.get("averageprice", 0) or 0),
                        ltp=float(h.get("ltp", 0) or 0),
                    )
                    holdings_list.append(row)
                    total_invested += row.qty * row.avg_price
                    current_value += row.qty * row.ltp
                holdings_list.sort(key=lambda r: (r.qty * r.ltp) - (r.qty * r.avg_price), reverse=True)
                total_pnl = current_value - total_invested
                total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0.0
        except Exception as e:
            logger.warning("Holdings fetch error: %s", e)

        try:
            pos = client.get_positions()
            if pos.get("status") and pos.get("data"):
                day_pnl = sum(float(p.get("pnl", 0) or 0) for p in pos["data"])
        except Exception as e:
            logger.warning("Positions error: %s", e)

        try:
            funds = client.get_funds()
            if funds.get("status") and funds.get("data"):
                d = funds["data"]
                available_cash = d.get("availablecash", "N/A")
                net_value = d.get("net", "N/A")
        except Exception as e:
            logger.warning("Funds error: %s", e)

        ctx.update(
            total_invested=total_invested,
            current_value=current_value,
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            day_pnl=day_pnl,
            available_cash=available_cash,
            net_value=net_value,
            holdings=holdings_list,
            client_id=client.client_id,
        )
    except Exception as e:
        ctx["error"] = str(e)
        ctx.update(total_invested=0, current_value=0, total_pnl=0, total_pnl_pct=0,
                   day_pnl=0, available_cash="N/A", net_value="N/A", holdings=[],
                   client_id=getattr(client, "client_id", ""))

    return templates.TemplateResponse(request, "dashboard.html", ctx)


@web.get("/positions", response_class=HTMLResponse)
async def positions_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)

    ctx = _ctx(request, "positions")
    pos_list = []
    total_pnl = 0.0

    try:
        data = client.get_positions()
        if data.get("status") and data.get("data"):
            for p in data["data"]:
                pnl = float(p.get("pnl", 0) or 0)
                total_pnl += pnl
                pos_list.append(_PositionRow(
                    symbol=p.get("tradingsymbol", "N/A"),
                    product=p.get("producttype", "N/A"),
                    net_qty=int(p.get("netqty", 0) or 0),
                    buy_avg=float(p.get("buyavgprice", 0) or 0),
                    sell_avg=float(p.get("sellavgprice", 0) or 0),
                    ltp=float(p.get("ltp", 0) or 0),
                    pnl=pnl,
                ))
    except Exception as e:
        ctx["error"] = str(e)

    ctx.update(positions=pos_list, total_pnl=total_pnl)
    return templates.TemplateResponse(request, "positions.html", ctx)


@web.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)

    ctx = _ctx(request, "orders")
    order_list = []
    trade_list = []

    try:
        data = client.get_order_book()
        if data.get("status") and data.get("data"):
            for o in data["data"]:
                order_list.append(_OrderRow(
                    orderid=o.get("orderid", "N/A"),
                    symbol=o.get("tradingsymbol", "N/A"),
                    txn_type=o.get("transactiontype", "N/A"),
                    qty=int(o.get("quantity", 0) or 0),
                    price=float(o.get("price", 0) or 0),
                    status=o.get("status", "N/A"),
                    time=o.get("updatetime", "N/A"),
                ))
    except Exception as e:
        ctx["error"] = str(e)

    try:
        tdata = client.get_trade_book()
        if tdata.get("status") and tdata.get("data"):
            for t in tdata["data"]:
                trade_list.append(_TradeRow(
                    tradeid=t.get("tradeid", "N/A"),
                    orderid=t.get("orderid", "N/A"),
                    symbol=t.get("tradingsymbol", "N/A"),
                    txn_type=t.get("transactiontype", "N/A"),
                    qty=int(t.get("fillsize", 0) or t.get("quantity", 0) or 0),
                    price=float(t.get("fillprice", 0) or t.get("price", 0) or 0),
                    time=t.get("filltime", t.get("updatetime", "N/A")),
                    exchange=t.get("exchange", "N/A"),
                    product=t.get("producttype", "N/A"),
                ))
    except Exception as e:
        ctx["trade_error"] = str(e)

    ctx.update(orders=order_list, trades=trade_list)
    return templates.TemplateResponse(request, "orders.html", ctx)


@web.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "analytics.html", _ctx(request, "analytics"))


@web.get("/research", response_class=HTMLResponse)
async def research_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "research.html", _ctx(request, "research"))


@web.get("/sectors", response_class=HTMLResponse)
async def sectors_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "sectors.html", _ctx(request, "sectors"))


@web.get("/news", response_class=HTMLResponse)
async def news_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "news.html", _ctx(request, "news"))


@web.get("/agent", response_class=HTMLResponse)
async def agent_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    _ensure_adk_chat_session_id(request)
    return templates.TemplateResponse(request, "agent.html", _ctx(request, "agent"))


_portfolio_cache: dict[str, tuple[float, dict]] = {}
_PORTFOLIO_CACHE_TTL = 60  # seconds

def _get_portfolio_cached(sid: str, client) -> dict:
    """Return portfolio data, using a short-lived cache to avoid Angel rate limits."""
    entry = _portfolio_cache.get(sid)
    if entry and (time.time() - entry[0]) < _PORTFOLIO_CACHE_TTL:
        return entry[1]
    data = _build_portfolio_data(client)
    _portfolio_cache[sid] = (time.time(), data)
    return data


@web.get("/trading", response_class=HTMLResponse)
async def trading_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    _ensure_adk_chat_session_id(request, _ADK_TRADING_CHAT_SESSION_KEY)
    sid = _sid(request)
    ctx = _ctx(request, "trading")
    ctx["has_risk_profile"] = risk_profiles.has(sid) if sid else False
    ctx["ws_sid"] = sid or ""
    portfolio = _get_portfolio_cached(sid, client)
    ctx["holdings_json"] = json.dumps(portfolio.get("holdings", []))
    return templates.TemplateResponse(request, "trading.html", ctx)


# ── News API (gnews) ──────────────────────────────────────────────────────


@web.get("/api/news/portfolio")
async def api_news_portfolio(
    request: Request,
    period: str = Query("7d"),
):
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    period = normalize_news_period(period)
    try:
        result = await asyncio.to_thread(build_portfolio_sector_news, client, period)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Portfolio news API error")
        return JSONResponse({"error": str(e)}, status_code=500)


@web.get("/api/news/search")
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


@web.post("/api/news/sentiment")
async def api_news_sentiment(request: Request):
    """
    Run FinBERT sentiment analysis on portfolio news.
    Body: {"sectors": [{"name": str, "invested": float, "news": [...]}]}
    Returns same structure with sentiment added per article and per sector.
    """
    client = _require_login(request)
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


# ── Portfolio Data API ─────────────────────────────────────────────────────


@web.get("/api/portfolio/analytics")
async def api_portfolio_analytics(request: Request):
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sid = _sid(request)
    return JSONResponse(_get_portfolio_cached(sid, client))


@web.get("/api/portfolio/candles")
async def api_portfolio_candles(
    request: Request,
    symbol: str = Query(...),
    exchange: str = Query("NSE"),
    interval: str = Query("ONE_DAY"),
    days: int = Query(90),
):
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        sr = _search_scrip_cached(client, exchange, _scrip_search_key(symbol))
        row = _pick_scrip_row(sr.get("data") if sr.get("status") else None, symbol)
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


@web.get("/api/portfolio/predict")
async def api_portfolio_predict(
    request: Request,
    symbol: str = Query(...),
    exchange: str = Query("NSE"),
    days: int = Query(365),
):
    """Predict price direction for a stock across multiple timeframes (up to 1 year)."""
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        sr = _search_scrip_cached(client, exchange, _scrip_search_key(symbol))
        row = _pick_scrip_row(sr.get("data") if sr.get("status") else None, symbol)
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
        prediction = await asyncio.to_thread(predict_direction, candles, symbol)
        return JSONResponse(prediction)
    except Exception as e:
        logger.exception("Prediction error for %s", symbol)
        return JSONResponse({"error": str(e)}, status_code=500)


@web.get("/api/portfolio/beta")
async def api_portfolio_beta(request: Request, days: int = Query(90)):
    """Compute portfolio beta vs NIFTY 50 using daily returns."""
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        portfolio = _build_portfolio_data(client)
        holdings = portfolio.get("holdings", [])
        if not holdings:
            return JSONResponse({"error": "No holdings found"}, status_code=400)

        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=days)
        from_str = from_dt.strftime("%Y-%m-%d 09:15")
        to_str = to_dt.strftime("%Y-%m-%d 15:30")

        nifty_token = "99926000"
        nifty_candles = _fetch_candles_safe(
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
                sr = _search_scrip_cached(client, "NSE", _scrip_search_key(sym))
                row = _pick_scrip_row(sr.get("data") if sr and sr.get("status") else None, sym)
                if not row or not row.get("symboltoken"):
                    continue
                token = row["symboltoken"]

                time.sleep(0.35)
                candles = _fetch_candles_safe(
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
                    sb = _compute_beta(
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

        result = _compute_beta(nifty_ret_list, port_returns)
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


# ── Research & Sector API endpoints ───────────────────────────────────────


@web.get("/api/research/fundamental")
async def api_research_fundamental(
    request: Request,
    symbol: str = Query(...),
):
    """Fetch fundamental analysis for a single stock."""
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        result = await asyncio.to_thread(get_stock_fundamentals, symbol)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Fundamental API error for %s", symbol)
        return JSONResponse({"error": str(e)}, status_code=500)


@web.get("/api/research/technical")
async def api_research_technical(
    request: Request,
    symbol: str = Query(...),
    exchange: str = Query("NSE"),
    days: int = Query(365),
):
    """Fetch technical indicators for a single stock using Angel One candle data."""
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        sr = _search_scrip_cached(client, exchange, _scrip_search_key(symbol))
        row = _pick_scrip_row(sr.get("data") if sr.get("status") else None, symbol)
        token = row.get("symboltoken") if row else None

        if not token:
            return JSONResponse({"error": f"Could not find token for {symbol}"}, status_code=404)

        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=min(days, 365))
        candles = _fetch_candles_safe(
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


@web.get("/api/sectors/overview")
async def api_sectors_overview(request: Request):
    """Get sector-level analysis for the user's portfolio."""
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        portfolio = _build_portfolio_data(client)
        holdings = portfolio.get("holdings", [])
        if not holdings:
            return JSONResponse({"error": "No holdings found"}, status_code=400)

        result = await asyncio.to_thread(get_sector_overview, holdings, SECTOR_MAP)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Sectors overview API error")
        return JSONResponse({"error": str(e)}, status_code=500)


@web.get("/api/sectors/breadth")
async def api_sectors_breadth(request: Request):
    """Get market breadth indicators."""
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        result = await asyncio.to_thread(get_market_breadth)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Market breadth API error")
        return JSONResponse({"error": str(e)}, status_code=500)


def _fetch_candles_safe(client, exchange, token, interval, from_str, to_str):
    try:
        result = client.get_candle_data({
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_str,
            "todate": to_str,
        })
        if result and result.get("status") and result.get("data"):
            return result["data"]
    except Exception as e:
        logger.warning("Candle fetch failed for token %s: %s", token, e)
    return None


def _compute_beta(x_returns, y_returns):
    n = len(x_returns)
    mean_x = sum(x_returns) / n
    mean_y = sum(y_returns) / n

    cov = sum((x_returns[i] - mean_x) * (y_returns[i] - mean_y) for i in range(n)) / n
    var_x = sum((x_returns[i] - mean_x) ** 2 for i in range(n)) / n

    beta = cov / var_x if var_x > 0 else 1.0
    alpha = mean_y - beta * mean_x

    ss_res = sum((y_returns[i] - (alpha + beta * x_returns[i])) ** 2 for i in range(n))
    ss_tot = sum((y_returns[i] - mean_y) ** 2 for i in range(n))
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return {"beta": round(beta, 4), "alpha": round(alpha, 6), "r_squared": round(r_squared, 4)}


# ── AI API endpoints ──────────────────────────────────────────────────────


def _build_portfolio_data(client) -> dict:
    """Extract holdings, positions, and funds into a dict for the LLM."""
    data = {"holdings": [], "summary": {}, "funds": {}}

    try:
        h_data = client.get_holdings()
        if h_data.get("status") and h_data.get("data"):
            total_inv = 0.0
            total_cur = 0.0
            for h in h_data["data"]:
                qty = int(h.get("quantity", 0) or 0)
                avg = float(h.get("averageprice", 0) or 0)
                ltp = float(h.get("ltp", 0) or 0)
                inv = qty * avg
                cur = qty * ltp
                pnl = cur - inv
                pnl_pct = (pnl / inv * 100) if inv else 0.0
                total_inv += inv
                total_cur += cur
                tok = h.get("symboltoken") or h.get("symbolToken") or ""
                data["holdings"].append({
                    "symbol": h.get("tradingsymbol", "N/A"),
                    "symboltoken": str(tok).strip() if tok else "",
                    "qty": qty, "avg_price": avg, "ltp": ltp,
                    "invested": round(inv, 2), "current": round(cur, 2),
                    "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                })
            data["summary"]["total_invested"] = round(total_inv, 2)
            data["summary"]["current_value"] = round(total_cur, 2)
            data["summary"]["overall_pnl"] = round(total_cur - total_inv, 2)
            data["summary"]["overall_pnl_pct"] = round(
                ((total_cur - total_inv) / total_inv * 100) if total_inv else 0.0, 2
            )
    except Exception as e:
        logger.warning("Portfolio build – holdings error: %s", e)

    try:
        pos = client.get_positions()
        if pos.get("status") and pos.get("data"):
            data["summary"]["day_pnl"] = round(
                sum(float(p.get("pnl", 0) or 0) for p in pos["data"]), 2
            )
    except Exception as e:
        logger.warning("Portfolio build – positions error: %s", e)

    try:
        funds = client.get_funds()
        if funds.get("status") and funds.get("data"):
            d = funds["data"]
            data["funds"]["available_cash"] = d.get("availablecash", "N/A")
            data["funds"]["net"] = d.get("net", "N/A")
    except Exception as e:
        logger.warning("Portfolio build – funds error: %s", e)

    return data


@web.post("/api/ai/insights")
async def ai_insights(request: Request):
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    api_key = body.get("api_key", "")
    if not api_key:
        return JSONResponse(
            {"error": "Please enter your OpenRouter API key"}, status_code=400
        )

    model = body.get("model", DEFAULT_OPENROUTER_MODEL)
    portfolio = _build_portfolio_data(client)

    try:
        insight = await generate_insights(api_key, portfolio, model)
        return JSONResponse({"insight": insight})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("AI insights error")
        return JSONResponse({"error": f"Something went wrong: {e}"}, status_code=500)


@web.post("/api/agent/chat")
async def api_agent_chat(request: Request):
    """Run an ADK agent (finance or trading). Uses Angel session from web login."""
    sid = _sid(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    agent_type = (body.get("agent_type") or "finance").strip().lower()
    debug = bool(body.get("debug"))

    if agent_type == "trading":
        session_key = _ADK_TRADING_CHAT_SESSION_KEY
    else:
        session_key = _ADK_CHAT_SESSION_KEY
    adk_session_id = _ensure_adk_chat_session_id(request, session_key)

    approval_result = None
    if agent_type == "trading":
        approval_result = _try_handle_approval(sid, message)

    effective_message = message
    if approval_result:
        effective_message = f"[System: {approval_result}] {message}"

    try:
        result = await registry.chat(
            angel_sid=sid,
            adk_session_id=adk_session_id,
            message=effective_message,
            agent_type=agent_type,
            debug=debug,
        )
        if approval_result:
            result["approval_result"] = approval_result
        return JSONResponse(result)
    except ValueError as e:
        err = str(e)
        if "OPENROUTER_API_KEY" in err:
            return JSONResponse({"error": err}, status_code=503)
        return JSONResponse({"error": err}, status_code=400)
    except Exception as e:
        logger.exception("ADK agent chat error")
        return JSONResponse({"error": f"Agent error: {e}"}, status_code=500)


def _try_handle_approval(sid: str, message: str) -> str | None:
    """If message is APPROVE/REJECT <id>, handle it server-side and return a status string."""
    m = _APPROVE_RE.match(message.strip())
    if m:
        pid = m.group(1)
        try:
            client = sessions.get_client(sid)
            proposal_store.approve(sid, pid)
            if client:
                result = execute_proposal(sid, pid, client)
                if result.get("ok"):
                    return f"Proposal {pid} APPROVED and executed. Order ID: {result.get('order_id')}"
                return f"Proposal {pid} APPROVED but execution failed: {result.get('error')}"
            return f"Proposal {pid} approved but no broker session found."
        except (ValueError, PermissionError) as e:
            return f"Approval failed: {e}"

    m = _REJECT_RE.match(message.strip())
    if m:
        pid = m.group(1)
        try:
            proposal_store.reject(sid, pid)
            return f"Proposal {pid} REJECTED."
        except (ValueError, PermissionError) as e:
            return f"Rejection failed: {e}"

    return None


@web.post("/api/agent/new-chat")
async def api_agent_new_chat(request: Request):
    """Start a fresh ADK thread (new id in the signed session cookie)."""
    sid = _sid(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    agent_type = (body.get("agent_type") or "finance").strip().lower()

    if agent_type == "trading":
        request.session[_ADK_TRADING_CHAT_SESSION_KEY] = uuid.uuid4().hex
    else:
        request.session[_ADK_CHAT_SESSION_KEY] = uuid.uuid4().hex
    return JSONResponse({"ok": True})


@web.post("/api/ai/ask")
async def ai_ask(request: Request):
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    api_key = body.get("api_key", "")
    if not api_key:
        return JSONResponse(
            {"error": "Please enter your OpenRouter API key"}, status_code=400
        )

    question = body.get("question", "")
    if not question:
        return JSONResponse({"error": "Question cannot be empty"}, status_code=400)

    model = body.get("model", DEFAULT_OPENROUTER_MODEL)
    portfolio = _build_portfolio_data(client)

    try:
        answer = await ask_question(api_key, question, portfolio, model)
        return JSONResponse({"answer": answer})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("AI ask error")
        return JSONResponse({"error": f"Something went wrong: {e}"}, status_code=500)


# ── Trading API ────────────────────────────────────────────────────────────


@web.get("/api/trading/profile")
async def api_trading_profile_get(request: Request):
    sid = _sid(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    profile = risk_profiles.get(sid)
    if profile is None:
        return JSONResponse({"has_profile": False})
    return JSONResponse({"has_profile": True, "profile": profile.to_dict()})


@web.post("/api/trading/profile")
async def api_trading_profile_set(request: Request):
    sid = _sid(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    try:
        profile = build_profile_from_dict(body)
        risk_profiles.set(sid, profile)
        return JSONResponse({"ok": True, "profile": profile.to_dict()})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@web.get("/api/trading/proposals")
async def api_trading_proposals(request: Request):
    sid = _sid(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    proposals = proposal_store.list_for_session(sid)
    return JSONResponse({
        "proposals": [p.to_dict() for p in proposals],
        "count": len(proposals),
    })


@web.post("/api/trading/proposals/{proposal_id}/approve")
async def api_trading_approve(request: Request, proposal_id: str):
    sid = _sid(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    client = sessions.get_client(sid)
    try:
        proposal_store.approve(sid, proposal_id)
        result = execute_proposal(sid, proposal_id, client)
        return JSONResponse(result)
    except (ValueError, PermissionError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("Proposal approval error")
        return JSONResponse({"error": str(e)}, status_code=500)


@web.post("/api/trading/proposals/{proposal_id}/reject")
async def api_trading_reject(request: Request, proposal_id: str):
    sid = _sid(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        proposal = proposal_store.reject(sid, proposal_id)
        return JSONResponse({"ok": True, "status": proposal.effective_status})
    except (ValueError, PermissionError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ── Real-time market WebSocket ─────────────────────────────────────────────


@web.websocket("/ws/market/{symbol}")
async def ws_market(websocket: WebSocket, symbol: str):
    """
    Push real-time price ticks for a symbol to the browser.
    Auth via ``?sid=`` query param (the actual Angel session id, injected by
    the template from the server-side session — never the raw cookie string).
    """
    await websocket.accept()

    ws_sid = websocket.query_params.get("sid", "").strip()
    if not ws_sid:
        await websocket.send_json({"error": "Not authenticated — sid missing"})
        await websocket.close()
        return

    client = sessions.get_client(ws_sid)
    if client is None:
        await websocket.send_json({"error": "Session expired"})
        await websocket.close()
        return

    exchange = "NSE"
    q_token = (websocket.query_params.get("symboltoken") or "").strip()
    tradingsymbol = symbol.strip()
    symboltoken: str | None = None

    if q_token and q_token.isdigit():
        symboltoken = q_token
    else:
        search_key = _scrip_search_key(symbol)
        try:
            scrip = await asyncio.to_thread(_search_scrip_cached, client, exchange, search_key)
        except Exception as e:
            msg = str(e).lower()
            logger.warning("search_scrip failed in ws_market for %s: %s", symbol, e)
            if "exceeding access rate" in msg or "access denied" in msg:
                detail = (
                    "Angel One rate limit: too many API calls in a short window. "
                    "Wait 30–60 seconds, then reload the page or pick the symbol again."
                )
            else:
                detail = "Symbol lookup failed (broker error). Try again in a moment."
            await websocket.send_json({"error": detail})
            await websocket.close()
            return

        if not isinstance(scrip, dict) or not scrip.get("status") or not scrip.get("data"):
            await websocket.send_json({"error": f"Symbol '{symbol}' not found on {exchange}"})
            await websocket.close()
            return

        match = _pick_scrip_row(scrip["data"], symbol)
        if not match or not match.get("symboltoken"):
            await websocket.send_json({"error": f"Could not resolve token for '{symbol}'"})
            await websocket.close()
            return
        symboltoken = str(match["symboltoken"])
        tradingsymbol = match["tradingsymbol"]

    await websocket.send_json({"status": "subscribed", "tradingsymbol": tradingsymbol, "symboltoken": symboltoken})

    try:
        async for tick in poll_ltp_fallback(client, exchange, tradingsymbol, symboltoken, interval=2.0):
            try:
                await websocket.send_json(tick.to_dict())
            except WebSocketDisconnect:
                break
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WS market error for %s: %s", symbol, e)


# ── Data classes for template rendering ────────────────────────────────────


@dataclass
class _HoldingRow:
    symbol: str
    qty: int
    avg_price: float
    ltp: float


@dataclass
class _PositionRow:
    symbol: str
    product: str
    net_qty: int
    buy_avg: float
    sell_avg: float
    ltp: float
    pnl: float


@dataclass
class _OrderRow:
    orderid: str
    symbol: str
    txn_type: str
    qty: int
    price: float
    status: str
    time: str


@dataclass
class _TradeRow:
    tradeid: str
    orderid: str
    symbol: str
    txn_type: str
    qty: int
    price: float
    time: str
    exchange: str
    product: str
