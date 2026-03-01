import os
import uuid
import logging
from dataclasses import dataclass

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ai_service import generate_insights, ask_question

from session_manager import sessions

logger = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))

web = FastAPI(docs_url=None, redoc_url=None)
web.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", uuid.uuid4().hex),
)
web.mount("/static", StaticFiles(directory=os.path.join(_dir, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_dir, "templates"))


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

    ctx.update(orders=order_list)
    return templates.TemplateResponse("orders.html", ctx)


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
                data["holdings"].append({
                    "symbol": h.get("tradingsymbol", "N/A"),
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
        return JSONResponse({"error": "Please enter your OpenAI API key"}, status_code=400)

    model = body.get("model", "gpt-4o-mini")
    portfolio = _build_portfolio_data(client)

    try:
        insight = await generate_insights(api_key, portfolio, model)
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
        return JSONResponse({"error": "Please enter your OpenAI API key"}, status_code=400)

    question = body.get("question", "")
    if not question:
        return JSONResponse({"error": "Question cannot be empty"}, status_code=400)

    model = body.get("model", "gpt-4o-mini")
    portfolio = _build_portfolio_data(client)

    try:
        answer = await ask_question(api_key, question, portfolio, model)
        return JSONResponse({"answer": answer})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("AI ask error")
        return JSONResponse({"error": f"Something went wrong: {e}"}, status_code=500)


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
