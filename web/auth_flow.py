"""The login, enrollment and logout flows, independent of how they are shown.

Two front ends drive these: the server-rendered forms in
:mod:`web.routers.auth` and the JSON API in :mod:`web.routers.auth_api` that the
React app calls. Keeping the flows here means the security-sensitive parts —
attempt throttling, the deliberately vague failure message, proving the broker
credentials before anything is written — exist exactly once. A router decides
only how to present an :class:`AuthFlowError`: re-render its form, or answer
with JSON.

This lives beside :mod:`web.dependencies` rather than in a router because
routers may not import each other (``tests/test_app_wiring.py`` enforces it).
"""
from __future__ import annotations

import logging

from fastapi import Request

from adminApi import service as admin_service
from db.passwords import MIN_PASSWORD_CHARS
from services import login_throttle
from services.realtime_feed import feed_relay
from session_manager import sessions
from web import country_codes
from web.dependencies import client_ip, session_id, start_session

logger = logging.getLogger(__name__)


class AuthFlowError(Exception):
    """A failed login or enrollment, carrying a message meant for the user.

    ``status_code`` is what a JSON caller should answer with. ``field`` names
    the input at fault when there is exactly one, so a form can mark it.
    ``retry_after_sec`` is set only for throttling.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        *,
        field: str | None = None,
        retry_after_sec: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.field = field
        self.retry_after_sec = retry_after_sec


def _normalized_number(country_code: str, whatsapp_number: str) -> str:
    try:
        return country_codes.normalize_whatsapp(country_code, whatsapp_number)
    except ValueError as e:
        raise AuthFlowError(str(e), 400, field="whatsapp_number") from None


def login(request: Request, country_code: str, whatsapp_number: str, app_password: str):
    """Start a session from a WhatsApp number and app password.

    Returns the live broker client. Raises :class:`AuthFlowError` otherwise.
    """
    number = _normalized_number(country_code, whatsapp_number)
    ip = client_ip(request)

    try:
        login_throttle.check(number, ip)
    except login_throttle.TooManyAttempts as e:
        raise AuthFlowError(str(e), 429, retry_after_sec=e.retry_after_sec) from None

    try:
        creds = admin_service.credentials_for_login(number, app_password)
    except Exception as e:
        # Almost always a missing or rotated ENCRYPTION_KEY, which no amount of
        # retrying fixes, so say something a user can act on.
        logger.exception("Login failed while reading stored credentials")
        raise AuthFlowError(f"Could not read your stored credentials: {e}", 500) from None

    if creds is None:
        # Deliberately one message for every cause — wrong number, wrong
        # password, or a premium row with no app password set yet. Naming which
        # would tell an attacker whether a number is registered.
        raise AuthFlowError(
            "That WhatsApp number and password don't match. If you registered "
            "before app passwords existed, set one up on the enroll page.",
            401,
        )

    try:
        _sid, client = start_session(request, creds)
    except Exception as e:
        logger.exception("Angel One login failed for stored credentials")
        raise AuthFlowError(
            "Your password was correct, but Angel One rejected the stored "
            f"credentials: {e}. Re-enroll to update them.",
            502,
        ) from None

    login_throttle.clear(number, ip)
    return client


def enroll(
    request: Request,
    *,
    api_key: str,
    client_id: str,
    password: str,
    totp_secret: str,
    country_code: str,
    whatsapp_number: str,
    app_password: str,
    app_password_confirm: str,
) -> dict:
    """Validate the Angel credentials, then store them against an app password.

    The broker call happens *before* the database write on purpose: a typo in
    the TOTP secret then fails here, with the form still in front of the user,
    instead of being stored and failing mysteriously at some later login.

    Returns the stored user's public view and leaves the request logged in.
    """
    number = _normalized_number(country_code, whatsapp_number)

    if app_password != app_password_confirm:
        raise AuthFlowError(
            "The two passwords do not match.", 400, field="app_password_confirm"
        )
    if len(app_password) < MIN_PASSWORD_CHARS:
        raise AuthFlowError(
            f"Choose an app password of at least {MIN_PASSWORD_CHARS} characters. "
            "It will be the only thing standing in front of your trading account.",
            400,
            field="app_password",
        )

    api_key = api_key.strip()
    client_id = client_id.strip()
    totp_secret = totp_secret.strip()

    # Step 1: prove the broker credentials by actually logging in with them.
    try:
        sid, _client = start_session(
            request,
            admin_service.AngelCredentials(
                user_id=0,  # replaced below once the row id is known
                api_key=api_key,
                client_id=client_id,
                password=password,
                totp_secret=totp_secret,
            ),
        )
    except Exception as e:
        logger.exception("Enrollment rejected: Angel One login failed")
        raise AuthFlowError(f"Angel One rejected these credentials: {e}", 400) from None

    # Step 2: store them, keyed to the chosen app password.
    try:
        existing = admin_service.find_by_client_id(client_id)
        claimed = admin_service.find_by_whatsapp_number(number)

        if claimed and (not existing or claimed["id"] != existing["id"]):
            raise AuthFlowError(
                "That WhatsApp number is already in use by another Angel One "
                "account. Use a different number, or log in instead.",
                409,
                field="whatsapp_number",
            )

        if existing:
            # Re-enrolling an existing row — which is also how a user registered
            # through premium (and therefore holding no app password) sets one.
            # Safe because reaching here required proving the Angel credentials.
            view = admin_service.update_user(
                existing["id"],
                whatsapp_number=number,
                angel_api_key=api_key,
                angel_password=password,
                angel_totp_secret=totp_secret,
                app_password=app_password,
                is_active=True,
            )
        else:
            view = admin_service.create_user(
                whatsapp_number=number,
                angel_api_key=api_key,
                angel_client_id=client_id,
                angel_password=password,
                angel_totp_secret=totp_secret,
                app_password=app_password,
            )
    except Exception as e:
        # The broker session is live but unsaved; drop it rather than leaving a
        # logged-in user whose next page load cannot find a row to rebuild from.
        sessions.remove_session(sid)
        request.session.clear()
        if isinstance(e, AuthFlowError):
            raise
        logger.exception("Enrollment failed during the database write")
        raise AuthFlowError(str(e), 500) from None

    request.session["uid"] = view["id"]
    logger.info("enrollment complete for user id=%s", view["id"])
    return view


def logout(request: Request) -> None:
    """End the broker session and forget the cookie's user.

    Clears ``uid`` too, so the next request does not silently rebuild the
    session the user just ended.
    """
    sid = session_id(request)
    if sid:
        feed_relay.stop_feed(sid)
        sessions.remove_session(sid)
    request.session.clear()
