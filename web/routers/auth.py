"""Landing, setup, connect, enrollment and the login/logout cycle.

Two distinct flows live here, and the split is the point:

``/enroll`` is the one-time setup. It takes the four Angel One credentials,
proves them against the broker, encrypts them into the ``users`` row, and hashes
an app password of the user's choosing.

``/login`` is every day after that. It takes a WhatsApp number and that one app
password, decrypts the stored credentials, and builds the same in-memory
``AngelOneClient`` the four-field form used to build. Nothing downstream of
``session_manager`` can tell the difference, which is why no other router
changed.

The flows themselves live in :mod:`web.auth_flow`, shared with the JSON API in
:mod:`web.routers.auth_api`. The handlers here only re-render the form when a
flow raises.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db.passwords import MIN_PASSWORD_CHARS
from services import feedback as feedback_service
from web import auth_flow, country_codes
from web.dependencies import current_client, template_context
from web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Public routes ──────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    if current_client(request):
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



# ── Enrollment: one time, trades four secrets for one ──────────────────────


def _phone_context(ctx: dict, dial_code: str = "", national_number: str = "") -> dict:
    """Add the country dropdown, and whatever the user last typed, to ``ctx``."""
    ctx["countries"] = country_codes.options()
    ctx["selected_dial"] = (
        (dial_code or "").strip().lstrip("+") or country_codes.DEFAULT_DIAL_CODE
    )
    ctx["national_number"] = national_number
    return ctx



@router.get("/enroll", response_class=HTMLResponse)
async def enroll_page(request: Request):
    if current_client(request):
        return RedirectResponse("/dashboard", status_code=302)
    ctx = template_context(request, "enroll")
    ctx["min_password_chars"] = MIN_PASSWORD_CHARS
    return templates.TemplateResponse(request, "enroll.html", _phone_context(ctx))



@router.post("/enroll")
async def enroll_submit(
    request: Request,
    api_key: str = Form(...),
    client_id: str = Form(...),
    password: str = Form(...),
    totp_secret: str = Form(...),
    country_code: str = Form(...),
    whatsapp_number: str = Form(...),
    app_password: str = Form(...),
    app_password_confirm: str = Form(...),
):
    """Validate the Angel credentials, then store them against an app password.

    See :func:`web.auth_flow.enroll` for the order of operations and why.
    """
    try:
        auth_flow.enroll(
            request,
            api_key=api_key,
            client_id=client_id,
            password=password,
            totp_secret=totp_secret,
            country_code=country_code,
            whatsapp_number=whatsapp_number,
            app_password=app_password,
            app_password_confirm=app_password_confirm,
        )
    except auth_flow.AuthFlowError as e:
        ctx = template_context(request, "enroll")
        ctx["min_password_chars"] = MIN_PASSWORD_CHARS
        ctx["error"] = e.message
        # Echo back only the non-secret fields so the user retypes less.
        ctx["form"] = {
            "api_key": api_key.strip(),
            "client_id": client_id.strip(),
        }
        return templates.TemplateResponse(
            request, "enroll.html", _phone_context(ctx, country_code, whatsapp_number)
        )

    return RedirectResponse("/dashboard", status_code=302)



# ── Login: the everyday path ───────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_client(request):
        return RedirectResponse("/dashboard", status_code=302)
    ctx = _phone_context(template_context(request))
    return templates.TemplateResponse(request, "login.html", ctx)



@router.post("/login")
async def login_submit(
    request: Request,
    country_code: str = Form(...),
    whatsapp_number: str = Form(...),
    app_password: str = Form(...),
):
    try:
        auth_flow.login(request, country_code, whatsapp_number, app_password)
    except auth_flow.AuthFlowError as e:
        ctx = template_context(request)
        ctx["error"] = e.message
        return templates.TemplateResponse(
            request, "login.html", _phone_context(ctx, country_code, whatsapp_number)
        )

    return RedirectResponse("/dashboard", status_code=302)



@router.post("/logout")
async def logout(request: Request):
    auth_flow.logout(request)
    return RedirectResponse("/", status_code=302)
