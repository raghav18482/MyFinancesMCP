"""Server-rendered HTML pages for the signed-in dashboard."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse


from services.risk_profile import risk_profiles, load_profile
from services.portfolio_service import get_portfolio_cached


from web.dependencies import (
    ADK_TRADING_CHAT_SESSION_KEY,
    ensure_adk_chat_session_id,
    registered_user_for_session,
    require_login,
    session_id,
    template_context,
)
from web.templating import templates
from web.view_models import HoldingRow, order_rows, position_rows, trade_rows

logger = logging.getLogger(__name__)

router = APIRouter()



@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    client = require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)

    ctx = template_context(request, "dashboard")
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
                    row = HoldingRow(
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



@router.get("/positions", response_class=HTMLResponse)
async def positions_page(request: Request):
    client = require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)

    ctx = template_context(request, "positions")
    pos_list = []

    try:
        pos_list = position_rows(client.get_positions())
    except Exception as e:
        ctx["error"] = str(e)

    ctx.update(positions=pos_list, total_pnl=sum(p.pnl for p in pos_list))
    return templates.TemplateResponse(request, "positions.html", ctx)



@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    client = require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)

    ctx = template_context(request, "orders")
    order_list = []
    trade_list = []

    try:
        order_list = order_rows(client.get_order_book())
    except Exception as e:
        ctx["error"] = str(e)

    try:
        trade_list = trade_rows(client.get_trade_book())
    except Exception as e:
        ctx["trade_error"] = str(e)

    ctx.update(orders=order_list, trades=trade_list)
    return templates.TemplateResponse(request, "orders.html", ctx)



@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    client = require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "analytics.html", template_context(request, "analytics"))



@router.get("/research", response_class=HTMLResponse)
async def research_page(request: Request):
    client = require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    ctx = template_context(request, "research")
    # The AI summary card decrypts the saved OpenRouter key with the client id
    # as the passphrase, the same way the dashboard does.
    ctx["client_id"] = client.client_id
    return templates.TemplateResponse(request, "research.html", ctx)



@router.get("/learn", response_class=HTMLResponse)
async def learn_page(request: Request):
    """Glossary of every fundamental metric, rendered from data/metric_glossary.json."""
    client = require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "learn.html", template_context(request, "learn"))



@router.get("/sectors", response_class=HTMLResponse)
async def sectors_page(request: Request):
    client = require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "sectors.html", template_context(request, "sectors"))



@router.get("/news", response_class=HTMLResponse)
async def news_page(request: Request):
    client = require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "news.html", template_context(request, "news"))



@router.get("/agent", response_class=HTMLResponse)
async def agent_page(request: Request):
    client = require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    ensure_adk_chat_session_id(request)
    return templates.TemplateResponse(request, "agent.html", template_context(request, "agent"))



@router.get("/trading", response_class=HTMLResponse)
async def trading_page(request: Request):
    client = require_login(request)
    if client is None:
        return RedirectResponse("/login", status_code=302)
    sid = session_id(request)
    ctx = template_context(request, "trading")

    user = registered_user_for_session(client)
    is_premium = bool(user and user.is_active)
    ctx["is_premium_registered"] = is_premium
    ctx["registered_whatsapp"] = user.whatsapp_number if user else ""

    if is_premium:
        ensure_adk_chat_session_id(request, ADK_TRADING_CHAT_SESSION_KEY)
        if sid and not risk_profiles.has(sid) and user:
            _db_profile = load_profile(user.id)
            if _db_profile:
                risk_profiles.set(sid, _db_profile)
        ctx["has_risk_profile"] = risk_profiles.has(sid) if sid else False
        portfolio = get_portfolio_cached(sid, client)
        ctx["holdings_json"] = json.dumps(portfolio.get("holdings", []))
    else:
        ctx["has_risk_profile"] = False
        ctx["holdings_json"] = "[]"

    return templates.TemplateResponse(request, "trading.html", ctx)
