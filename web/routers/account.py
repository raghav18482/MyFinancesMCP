"""Account deletion (in the app and on the web) and the public legal pages.

The stores require both: an in-app way to delete an account the app created,
and — for Google Play — a web page where it can be requested without the app.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import account_deletion, login_throttle
from web import auth_flow, country_codes
from web.dependencies import client_ip, current_client, registered_user_for_session
from web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

_WRONG_PASSWORD = "That password isn't right."


@router.delete("/api/account")
async def api_account_delete(request: Request):
    """Delete the signed-in user's account after re-checking the app password.

    A wrong password is a 400 naming the field, not a 401: the session is
    still valid, and a 401 would sign the app out.
    """
    client = current_client(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    user = registered_user_for_session(client)
    if user is None:
        # Nothing stored for this session: there is nothing to delete.
        auth_flow.logout(request)
        return JSONResponse({"ok": True, "deleted": False})
    if not user.app_password_hash:
        return JSONResponse(
            {
                "error": "Set an app password first (re-run setup), then delete your account.",
                "field": "app_password",
            },
            status_code=400,
        )

    identifier = f"delete:{user.id}"
    ip = client_ip(request)
    try:
        login_throttle.check(identifier, ip)
    except login_throttle.TooManyAttempts as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=429,
            headers={"Retry-After": str(e.retry_after_sec)},
        )

    password = body.get("app_password")
    if not account_deletion.password_matches(user, password if isinstance(password, str) else ""):
        return JSONResponse({"error": _WRONG_PASSWORD, "field": "app_password"}, status_code=400)

    login_throttle.clear(identifier, ip)
    report = account_deletion.delete_user(user.id)
    await account_deletion.delete_conversations(report)
    auth_flow.logout(request)
    return JSONResponse({"ok": True, "deleted": True})


# ── Public pages ───────────────────────────────────────────────────────────


def _page(request: Request, name: str, *, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("contact_email", os.environ.get("LEGAL_CONTACT_EMAIL", "").strip())
    return templates.TemplateResponse(
        request, f"legal/{name}.html", ctx, status_code=status_code
    )


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return _page(request, "privacy")


@router.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return _page(request, "terms")


@router.get("/risk-disclosure", response_class=HTMLResponse)
async def risk_page(request: Request):
    return _page(request, "risk")


@router.get("/delete-account", response_class=HTMLResponse)
async def delete_account_page(request: Request):
    return _page(
        request,
        "delete_account",
        countries=country_codes.options(),
        default_dial=country_codes.DEFAULT_DIAL_CODE,
    )


@router.post("/delete-account", response_class=HTMLResponse)
async def delete_account_form(
    request: Request,
    country_code: str = Form(""),
    whatsapp_number: str = Form(""),
    app_password: str = Form(""),
    confirm: str = Form(""),
):
    """Deletion without the app: the same proof as signing in."""

    def answer(*, error: str | None = None, done: bool = False, status: int = 200):
        return _page(
            request,
            "delete_account",
            countries=country_codes.options(),
            default_dial=country_code or country_codes.DEFAULT_DIAL_CODE,
            error=error,
            done=done,
            status_code=status,
        )

    if confirm != "yes":
        return answer(error="Tick the box to confirm you want your account deleted.", status=400)
    try:
        number = country_codes.normalize_whatsapp(country_code, whatsapp_number)
    except ValueError as e:
        return answer(error=str(e), status=400)

    ip = client_ip(request)
    try:
        login_throttle.check(number, ip)
    except login_throttle.TooManyAttempts as e:
        return answer(error=str(e), status=429)

    user = account_deletion.user_by_number(number)
    if not account_deletion.password_matches(user, app_password) or user is None:
        # One message for every cause, as at sign-in.
        return answer(error="That WhatsApp number and password don't match.", status=400)

    login_throttle.clear(number, ip)
    report = account_deletion.delete_user(user.id)
    await account_deletion.delete_conversations(report)
    return answer(done=True)
