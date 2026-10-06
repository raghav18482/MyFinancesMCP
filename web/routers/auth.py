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
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from adminApi import service as admin_service
from db.passwords import MIN_PASSWORD_CHARS

from web import country_codes

from services.realtime_feed import feed_relay
from services import feedback as feedback_service
from services import login_throttle

from session_manager import sessions

from web.dependencies import (
    client_ip,
    current_client,
    session_id,
    start_session,
    template_context,
)
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

    The broker call happens *before* the database write on purpose: a typo in
    the TOTP secret then fails here, with the form still in front of the user,
    instead of being stored and failing mysteriously at some later login.
    """

    def fail(message: str):
        ctx = template_context(request, "enroll")
        ctx["min_password_chars"] = MIN_PASSWORD_CHARS
        ctx["error"] = message
        # Echo back only the non-secret fields so the user retypes less.
        ctx["form"] = {
            "api_key": api_key.strip(),
            "client_id": client_id.strip(),
        }
        return templates.TemplateResponse(
            request, "enroll.html", _phone_context(ctx, country_code, whatsapp_number)
        )

    try:
        number = country_codes.normalize_whatsapp(country_code, whatsapp_number)
    except ValueError as e:
        return fail(str(e))

    if app_password != app_password_confirm:
        return fail("The two passwords do not match.")
    if len(app_password) < MIN_PASSWORD_CHARS:
        return fail(
            f"Choose an app password of at least {MIN_PASSWORD_CHARS} characters. "
            "It will be the only thing standing in front of your trading account."
        )

    # Step 1: prove the broker credentials by actually logging in with them.
    sid = None
    try:
        sid = start_session(
            request,
            admin_service.AngelCredentials(
                user_id=0,  # replaced below once the row id is known
                api_key=api_key.strip(),
                client_id=client_id.strip(),
                password=password,
                totp_secret=totp_secret.strip(),
            ),
        )[0]
    except Exception as e:
        logger.exception("Enrollment rejected: Angel One login failed")
        return fail(f"Angel One rejected these credentials: {e}")

    # Step 2: store them, keyed to the chosen app password.
    try:
        existing = admin_service.find_by_client_id(client_id.strip())
        claimed = admin_service.find_by_whatsapp_number(number)

        if claimed and (not existing or claimed["id"] != existing["id"]):
            raise ValueError(
                "That WhatsApp number is already in use by another Angel One "
                "account. Use a different number, or log in instead."
            )

        if existing:
            # Re-enrolling an existing row — which is also how a user registered
            # through premium (and therefore holding no app password) sets one.
            # Safe because reaching here required proving the Angel credentials.
            view = admin_service.update_user(
                existing["id"],
                whatsapp_number=number,
                angel_api_key=api_key.strip(),
                angel_password=password,
                angel_totp_secret=totp_secret.strip(),
                app_password=app_password,
                is_active=True,
            )
        else:
            view = admin_service.create_user(
                whatsapp_number=number,
                angel_api_key=api_key.strip(),
                angel_client_id=client_id.strip(),
                angel_password=password,
                angel_totp_secret=totp_secret.strip(),
                app_password=app_password,
            )
    except Exception as e:
        # The broker session is live but unsaved; drop it rather than leaving a
        # logged-in user whose next page load cannot find a row to rebuild from.
        if sid:
            sessions.remove_session(sid)
        request.session.clear()
        logger.exception("Enrollment failed during the database write")
        return fail(str(e))

    request.session["uid"] = view["id"]
    logger.info("enrollment complete for user id=%s", view["id"])
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
    ip = client_ip(request)

    def fail(message: str):
        ctx = template_context(request)
        ctx["error"] = message
        return templates.TemplateResponse(
            request, "login.html", _phone_context(ctx, country_code, whatsapp_number)
        )

    try:
        number = country_codes.normalize_whatsapp(country_code, whatsapp_number)
    except ValueError as e:
        return fail(str(e))

    try:
        login_throttle.check(number, ip)
    except login_throttle.TooManyAttempts as e:
        return fail(str(e))

    try:
        creds = admin_service.credentials_for_login(number, app_password)
    except Exception as e:
        # Almost always a missing or rotated ENCRYPTION_KEY, which no amount of
        # retrying fixes, so say something a user can act on.
        logger.exception("Login failed while reading stored credentials")
        return fail(f"Could not read your stored credentials: {e}")

    if creds is None:
        # Deliberately one message for every cause — wrong number, wrong
        # password, or a premium row with no app password set yet. Naming which
        # would tell an attacker whether a number is registered.
        return fail(
            "That WhatsApp number and password don't match. If you registered "
            "before app passwords existed, set one up on the enroll page."
        )

    try:
        start_session(request, creds)
    except Exception as e:
        logger.exception("Angel One login failed for stored credentials")
        return fail(
            "Your password was correct, but Angel One rejected the stored "
            f"credentials: {e}. Re-enroll to update them."
        )

    login_throttle.clear(number, ip)
    return RedirectResponse("/dashboard", status_code=302)



@router.post("/logout")
async def logout(request: Request):
    sid = session_id(request)
    if sid:
        feed_relay.stop_feed(sid)
        sessions.remove_session(sid)
    # Clears ``uid`` too, so the next request does not silently rebuild the
    # session the user just ended.
    request.session.clear()
    return RedirectResponse("/", status_code=302)
