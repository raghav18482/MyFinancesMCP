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

from functools import partial

import anyio
from fastapi import Request

from adminApi import service as admin_service
from db.passwords import MIN_PASSWORD_CHARS
from services import login_throttle
from session_manager import sessions
from web import broker_sessions, country_codes
from web.dependencies import (
    bind_user,
    end_web_session,
    session_id,
    sign_out_everywhere,
    trusted_ip,
)

logger = logging.getLogger(__name__)

#: Enrollments (each a real Angel One login) allowed across all callers per window.
ENROLL_GLOBAL_CAP = 30


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


async def _broker_login(request: Request, key: str, creds):
    """Log in to Angel One off the event loop and point the cookie at the session.

    Concurrent logins under ``key`` share one broker login. Raises whatever the
    login raises (``angel_client.AngelAuthError``).
    """
    runtime = broker_sessions.get_runtime()
    result = await runtime.login(key, lambda: creds)
    if result is None:  # pragma: no cover - the loader always returns creds
        raise RuntimeError("no credentials to log in with")
    sid, client = result
    old_sid = session_id(request)
    request.session["sid"] = sid
    if old_sid and old_sid != sid:
        sessions.mark_for_release(old_sid)
    return sid, client


def _normalized_number(country_code: str, whatsapp_number: str) -> str:
    try:
        return country_codes.normalize_whatsapp(country_code, whatsapp_number)
    except ValueError as e:
        raise AuthFlowError(str(e), 400, field="whatsapp_number") from None


async def login(request: Request, country_code: str, whatsapp_number: str, app_password: str):
    """Start a session from a WhatsApp number and app password.

    Returns the live broker client. Raises :class:`AuthFlowError` otherwise.
    """
    number = _normalized_number(country_code, whatsapp_number)
    ip = trusted_ip(request)

    try:
        login_throttle.check(number, ip)
    except login_throttle.TooManyAttempts as e:
        raise AuthFlowError(str(e), 429, retry_after_sec=e.retry_after_sec) from None

    try:
        # scrypt is deliberately slow (~100 ms); keep it off the event loop too.
        creds = await anyio.to_thread.run_sync(
            partial(admin_service.credentials_for_login, number, app_password)
        )
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
        _sid, client = await _broker_login(request, f"user:{creds.user_id}", creds)
    except Exception as e:
        logger.exception("Angel One login failed for stored credentials")
        raise AuthFlowError(
            "Your password was correct, but Angel One rejected the stored "
            f"credentials: {e}. Re-enroll to update them.",
            502,
        ) from None

    bind_user(request, creds.user_id)
    login_throttle.clear(number, ip)
    return client


async def enroll(
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

    # Every enrollment makes a real Angel One login with whatever client id was
    # typed, so without a limit this form would let anyone drive failed logins
    # (and Angel's lockout) against someone else's account. Capped per client
    # id, per verified IP when one is known, and across all callers.
    enroll_key = f"enroll:{client_id.upper()}"
    ip = trusted_ip(request)
    try:
        login_throttle.check(enroll_key, ip)
        login_throttle.check_global("enroll", ENROLL_GLOBAL_CAP)
    except login_throttle.TooManyAttempts as e:
        raise AuthFlowError(str(e), 429, retry_after_sec=e.retry_after_sec) from None

    # Step 1: prove the broker credentials by actually logging in with them.
    try:
        sid, _client = await _broker_login(
            request,
            enroll_key,
            admin_service.AngelCredentials(
                user_id=0,  # the row id is bound below once it exists
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

    bind_user(request, view["id"])
    login_throttle.clear(enroll_key, ip)
    logger.info("enrollment complete for user id=%s", view["id"])
    return view


def logout(request: Request) -> None:
    """End the broker session and revoke every copy of this user's cookie.

    The cookie is signed but stateless, so clearing it here does nothing about
    a copy saved earlier. Bumping the user's ``session_version`` does: every
    cookie issued before now carries the old version and is refused from the
    next request on — on this device and any other. The broker sessions of
    every device, with their ADK runners and caches, are released now too.
    """
    uid = request.session.get("uid")
    if uid:
        try:
            admin_service.bump_session_version(int(uid))
        except Exception:
            # Still end this device's session; say so loudly, since saved copies
            # of the cookie stay valid until the bump succeeds.
            logger.exception("Logout: could not bump session version for uid=%s", uid)
        sign_out_everywhere(request, int(uid))
    end_web_session(request)
