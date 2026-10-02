"""Landing, setup, connect and the login/logout cycle."""
from __future__ import annotations

import uuid
import logging

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse


from services.realtime_feed import feed_relay
from services import feedback as feedback_service

from session_manager import sessions

from web.dependencies import (
    session_id,
    template_context,
)
from web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()



# ── Public routes ──────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    if session_id(request) and sessions.get_client(session_id(request)):
        return RedirectResponse("/dashboard", status_code=302)
    ctx = template_context(request)
    try:
        ctx["feedback_token"] = feedback_service.issue_feedback_token()
    except Exception:
        # ENCRYPTION_KEY missing/misconfigured: render page without the form token.
        logger.exception("Could not issue feedback token")
        ctx["feedback_token"] = ""
    return templates.TemplateResponse(request, "landing.html", ctx)



@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return templates.TemplateResponse(request, "setup.html", template_context(request, "setup"))



@router.get("/connect", response_class=HTMLResponse)
async def connect_page(request: Request):
    server_url = str(request.base_url).rstrip("/")
    ctx = template_context(request, "connect")
    ctx["server_url"] = server_url
    return templates.TemplateResponse(request, "connect.html", ctx)



@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if session_id(request) and sessions.get_client(session_id(request)):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "login.html", template_context(request))



@router.post("/login")
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
        ctx = template_context(request)
        ctx["error"] = f"Login failed: {e}"
        return templates.TemplateResponse(request, "login.html", ctx)



@router.post("/logout")
async def logout(request: Request):
    sid = session_id(request)
    if sid:
        feed_relay.stop_feed(sid)
        sessions.remove_session(sid)
    request.session.clear()
    return RedirectResponse("/", status_code=302)
