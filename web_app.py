import os
import json
import uuid
import time
import asyncio
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass

from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from gnews import GNews

from ai_service import generate_insights, ask_question
from llm_providers import default_model_for_provider, normalize_provider
from fundamental_service import get_stock_fundamentals
from technical_service import compute_technical_indicators
from sector_service import get_sector_overview, get_market_breadth
from sentiment_service import analyze_articles, compute_sector_sentiment

from session_manager import sessions
from portfolio_snapshot import build_portfolio_data as _build_portfolio_data

logger = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))

web = FastAPI(docs_url=None, redoc_url=None)
web.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", uuid.uuid4().hex),
)
web.mount("/static/data", StaticFiles(directory=os.path.join(_dir, "data")), name="data")
web.mount("/static", StaticFiles(directory=os.path.join(_dir, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_dir, "templates"))

with open(os.path.join(_dir, "data", "sector_map.json")) as _f:
    SECTOR_MAP: dict[str, str] = json.load(_f)

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


def _sid(request: Request) -> str | None:
    return request.session.get("sid")


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
    return templates.TemplateResponse("landing.html", _ctx(request))


@web.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return templates.TemplateResponse("setup.html", _ctx(request, "setup"))


@web.get("/connect", response_class=HTMLResponse)
async def connect_page(request: Request):
    server_url = str(request.base_url).rstrip("/")
    ctx = _ctx(request, "connect")
    ctx["server_url"] = server_url
    return templates.TemplateResponse("connect.html", ctx)


@web.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _sid(request) and sessions.get_client(_sid(request)):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", _ctx(request))


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
        return templates.TemplateResponse("login.html", ctx)


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

    return templates.TemplateResponse("dashboard.html", ctx)


@web.get("/holdings", response_class=HTMLResponse)
async def holdings_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)

    ctx = _ctx(request, "holdings")
    holdings_list = []
    total_invested = 0.0
    current_value = 0.0

    try:
        data = client.get_holdings()
        if data.get("status") and data.get("data"):
            for h in data["data"]:
                row = _HoldingRow(
                    symbol=h.get("tradingsymbol", "N/A"),
                    qty=int(h.get("quantity", 0) or 0),
                    avg_price=float(h.get("averageprice", 0) or 0),
                    ltp=float(h.get("ltp", 0) or 0),
                )
                holdings_list.append(row)
                total_invested += row.qty * row.avg_price
                current_value += row.qty * row.ltp
    except Exception as e:
        ctx["error"] = str(e)

    holdings_list.sort(key=lambda r: (r.qty * r.ltp) - (r.qty * r.avg_price), reverse=True)

    total_pnl = current_value - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0.0

    ctx.update(
        holdings=holdings_list,
        total_invested=total_invested,
        current_value=current_value,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
    )
    return templates.TemplateResponse("holdings.html", ctx)


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
    return templates.TemplateResponse("positions.html", ctx)


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
    return templates.TemplateResponse("orders.html", ctx)


@web.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("analytics.html", _ctx(request, "analytics"))


@web.get("/research", response_class=HTMLResponse)
async def research_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("research.html", _ctx(request, "research"))


@web.get("/sectors", response_class=HTMLResponse)
async def sectors_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("sectors.html", _ctx(request, "sectors"))


@web.get("/news", response_class=HTMLResponse)
async def news_page(request: Request):
    client = _require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("news.html", _ctx(request, "news"))


# ── News API (gnews) ──────────────────────────────────────────────────────

_VALID_PERIODS = {"1d", "7d", "1m", "3m", "6m", "1y"}
_MAX_SECTORS = 8


def _gnews_to_dict(article: dict) -> dict:
    """Normalise a gnews article dict into our frontend format."""
    publisher = article.get("publisher") or {}
    return {
        "title": article.get("title", ""),
        "link": article.get("url", "#"),
        "date": article.get("published date", ""),
        "description": article.get("description", ""),
        "source": publisher.get("title", "") if isinstance(publisher, dict) else str(publisher),
    }


def _fetch_sector_news(query: str, period: str, max_results: int) -> list[dict]:
    """Synchronous helper – runs in a thread. Fetches news for one sector."""
    try:
        gn = GNews(language="en", country="IN", period=period, max_results=max_results)
        raw = gn.get_news(query)
        return [_gnews_to_dict(a) for a in (raw or [])]
    except Exception as e:
        logger.warning("gnews query failed for %r: %s", query, e)
        return []


async def _build_portfolio_news(client, period: str = "7d") -> dict:
    """Get holdings, compute sector weights, query gnews per sector."""
    sector_invested: dict[str, float] = {}

    try:
        h_data = client.get_holdings()
        if h_data.get("status") and h_data.get("data"):
            for h in h_data["data"]:
                sym = h.get("tradingsymbol", "")
                qty = int(h.get("quantity", 0) or 0)
                avg = float(h.get("averageprice", 0) or 0)
                invested = qty * avg
                sector = SECTOR_MAP.get(sym, "Other")
                sector_invested[sector] = sector_invested.get(sector, 0) + invested
    except Exception as e:
        logger.warning("News – holdings fetch error: %s", e)

    sector_order = sorted(
        sector_invested.keys(), key=lambda s: sector_invested[s], reverse=True
    )[:_MAX_SECTORS]

    def _fetch_all_sectors():
        results: dict[str, list[dict]] = {}
        for sector in sector_order:
            query = SECTOR_QUERIES.get(sector, f"{sector} India stock market")
            results[sector] = _fetch_sector_news(query, period, 5)
            time.sleep(0.5)
        return results

    grouped = await asyncio.to_thread(_fetch_all_sectors)

    sectors_list = []
    for sector in sector_order:
        sectors_list.append({
            "name": sector,
            "invested": round(sector_invested.get(sector, 0), 2),
            "news": grouped.get(sector, []),
        })

    return {"sectors": sectors_list}


@web.get("/api/news/portfolio")
async def api_news_portfolio(
    request: Request,
    period: str = Query("7d"),
):
    client = _require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if period not in _VALID_PERIODS:
        period = "7d"
    try:
        result = await _build_portfolio_news(client, period)
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
    if period not in _VALID_PERIODS:
        period = "7d"

    def _do_search():
        gn = GNews(language="en", country="IN", period=period, max_results=20)
        if location.strip():
            raw = gn.get_news(f"{q.strip()} {location.strip()}")
        else:
            raw = gn.get_news(q.strip())
        return [_gnews_to_dict(a) for a in (raw or [])]

    try:
        articles = await asyncio.to_thread(_do_search)
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

    def _run_sentiment():
        enriched_sectors = []
        overall_bullish = 0
        overall_bearish = 0
        overall_neutral = 0

        for sector in sectors:
            articles = sector.get("news", [])
            analyzed = analyze_articles(articles)
            sector_agg = compute_sector_sentiment(analyzed)

            enriched_sectors.append({
                "name": sector.get("name", ""),
                "invested": sector.get("invested", 0),
                "news": analyzed,
                "sentiment_summary": sector_agg,
            })
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

    try:
        result = await asyncio.to_thread(_run_sentiment)
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
    return JSONResponse(_build_portfolio_data(client))


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
        sr = client.search_scrip(exchange, symbol.replace("-EQ", ""))
        token = None
        if sr.get("status") and sr.get("data"):
            for item in sr["data"]:
                if item.get("tradingsymbol") == symbol:
                    token = item["symboltoken"]
                    break
        if not token:
            return JSONResponse({"error": f"Could not find token for {symbol}"}, status_code=404)

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
                sr = client.search_scrip("NSE", sym.replace("-EQ", ""))
                token = None
                if sr and sr.get("status") and sr.get("data"):
                    for item in sr["data"]:
                        if item.get("tradingsymbol") == sym:
                            token = item["symboltoken"]
                            break
                if not token:
                    continue

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
        base_sym = symbol.replace("-EQ", "")
        sr = client.search_scrip(exchange, base_sym)
        token = None
        if sr.get("status") and sr.get("data"):
            for item in sr["data"]:
                if item.get("tradingsymbol") == symbol:
                    token = item["symboltoken"]
                    break
            if not token:
                token = sr["data"][0]["symboltoken"]

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
            {"error": "Please enter your OpenAI or Google AI (Gemini) API key"},
            status_code=400,
        )

    provider = normalize_provider(body.get("provider"))
    raw_model = body.get("model")
    model = (
        (raw_model.strip() if isinstance(raw_model, str) else "")
        or default_model_for_provider(provider)
    )
    portfolio = _build_portfolio_data(client)

    try:
        insight = await generate_insights(api_key, portfolio, model, provider=provider)
        return JSONResponse({"insight": insight})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("AI insights error")
        return JSONResponse({"error": f"Something went wrong: {e}"}, status_code=500)


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
            {"error": "Please enter your OpenAI or Google AI (Gemini) API key"},
            status_code=400,
        )

    question = body.get("question", "")
    if not question:
        return JSONResponse({"error": "Question cannot be empty"}, status_code=400)

    provider = normalize_provider(body.get("provider"))
    raw_model = body.get("model")
    model = (
        (raw_model.strip() if isinstance(raw_model, str) else "")
        or default_model_for_provider(provider)
    )
    portfolio = _build_portfolio_data(client)

    try:
        answer = await ask_question(api_key, question, portfolio, model, provider=provider)
        return JSONResponse({"answer": answer})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("AI ask error")
        return JSONResponse({"error": f"Something went wrong: {e}"}, status_code=500)


@web.post("/api/agent/langgraph/chat")
async def langgraph_agent_chat(request: Request):
    """LangGraph ReAct agent: broker tools, Chroma RAG, web search, SQLite memory."""
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
            {"error": "LLM API key required (OpenAI or Google AI / Gemini)"},
            status_code=400,
        )

    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    raw_tid = body.get("thread_id")
    thread_id = (raw_tid.strip() if isinstance(raw_tid, str) and raw_tid.strip() else None) or str(
        uuid.uuid4()
    )
    provider = normalize_provider(body.get("provider"))
    raw_model = body.get("model")
    model = (
        (raw_model.strip() if isinstance(raw_model, str) else "")
        or default_model_for_provider(provider)
    )
    sid = _sid(request)

    try:
        from agent_langgraph.runner import resolve_user_key, run_langgraph_agent

        user_key = resolve_user_key(client, sid)
        answer = await run_langgraph_agent(
            client=client,
            user_key=user_key,
            message=message,
            thread_id=thread_id,
            api_key=api_key,
            model=model,
            llm_provider=provider,
        )
        return JSONResponse({"answer": answer, "thread_id": thread_id})
    except ImportError as e:
        logger.error("LangGraph agent dependencies missing: %s", e)
        return JSONResponse(
            {
                "error": "Agent not available: install langgraph and related deps (see requirements.txt).",
            },
            status_code=503,
        )
    except Exception as e:
        logger.exception("LangGraph agent error")
        return JSONResponse({"error": str(e)}, status_code=500)


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
