"""The login, registration and logout flows, independent of how they are shown.

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
from db.models import BrokerageStatus, User
from db.passwords import MIN_PASSWORD_CHARS
from services import login_throttle
from session_manager import sessions
from web import country_codes
from web.dependencies import (
    bind_user,
    client_ip,
    current_app_user,
    end_web_session,
    start_session,
)

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


def account_payload(user: User) -> dict:
    """What every "who am I" answer carries: login, registration, bootstrap.

    No credential, token or hash ever goes in here. ``client_id`` is the Angel
    One account code (an identifier, not a secret), and only while connected.
    """
    connected = admin_service.is_connected(user)
    return {
        "user_id": user.id,
        "phone_number": user.whatsapp_number,
        "whatsapp_number": user.whatsapp_number,
        "client_id": user.angel_client_id if connected else None,
        "brokerage_status": user.brokerage_status,
        "available_features": admin_service.available_features(user),
    }


def login(request: Request, country_code: str, whatsapp_number: str, app_password: str) -> User:
    """Sign in with a WhatsApp number and app password. Returns the ``User``.

    The broker is not contacted: signing in to the app works the same whether
    or not a broker is connected, and whether or not Angel One is up. A
    connected user's broker session is built on their first brokerage request
    (:func:`web.dependencies.require_brokerage`). Raises :class:`AuthFlowError`.
    """
    number = _normalized_number(country_code, whatsapp_number)
    ip = client_ip(request)

    try:
        login_throttle.check(number, ip)
    except login_throttle.TooManyAttempts as e:
        raise AuthFlowError(str(e), 429, retry_after_sec=e.retry_after_sec) from None

    try:
        user = admin_service.authenticate_app_user(number, app_password)
    except Exception:
        logger.exception("Login failed while reading the user record")
        raise AuthFlowError("Sign-in is unavailable right now. Please try again shortly.", 500) from None

    if user is None:
        # Deliberately one message for every cause — wrong number, wrong
        # password, or a premium row with no app password set yet. Naming which
        # would tell an attacker whether a number is registered.
        raise AuthFlowError(
            "That WhatsApp number and password don't match. If you registered "
            "before app passwords existed, set one up on the enroll page.",
            401,
        )

    # Whatever this browser was signed in as before — another user's broker
    # session included — goes before the new identity is written.
    end_web_session(request)
    bind_user(request, user.id)
    login_throttle.clear(number, ip)
    return user


_BROKER_FIELDS = ("client_id", "api_key", "password", "totp_secret")


def enroll(
    request: Request,
    *,
    api_key: str = "",
    client_id: str = "",
    password: str = "",
    totp_secret: str = "",
    country_code: str,
    whatsapp_number: str,
    app_password: str,
    app_password_confirm: str,
) -> dict:
    """Register for the app, optionally connecting Angel One in the same step.

    - **No broker fields:** create an app account (phone number and password);
      ``brokerage_status`` is ``not_connected`` and the user connects later
      through ``POST /api/brokerage/register``.
    - **All four broker fields:** validate them with Angel One, then store them
      against the app password. This is also how a row created by premium
      registration, which has no app password, gets one.
    - **Some but not all:** rejected, so a half-filled form never silently
      creates an account without the broker the user meant to connect.

    In the second case the broker call happens *before* the database write on
    purpose: a typo in the TOTP secret then fails here, with the form still in
    front of the user, instead of being stored and failing at some later use.

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

    api_key = (api_key or "").strip()
    client_id = (client_id or "").strip()
    totp_secret = (totp_secret or "").strip()
    password = password or ""

    supplied = dict(zip(_BROKER_FIELDS, (client_id, api_key, password, totp_secret)))
    if not any(supplied.values()):
        return _register_app_user(request, number, app_password)
    missing = [name for name, value in supplied.items() if not value]
    if missing:
        raise AuthFlowError(
            "To connect Angel One now, fill in all four Angel One fields — or leave "
            "them all empty and connect your broker later.",
            400,
            field=missing[0],
        )

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
            if not claimed["angel_client_id"]:
                # An app account with no broker yet. Linking one to it needs that
                # account's password, i.e. logging in first — never this form.
                raise AuthFlowError(
                    "That WhatsApp number is already registered. Log in, then "
                    "connect Angel One from your account.",
                    409,
                    field="whatsapp_number",
                )
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
                brokerage_status=BrokerageStatus.CONNECTED,
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
    logger.info("enrollment complete for user id=%s", view["id"])
    return view


def _register_app_user(request: Request, number: str, app_password: str) -> dict:
    """Create an account with no broker and sign it in."""
    ip = client_ip(request)
    try:
        # Its own identifier, so registrations never lock a number out of
        # logging in; they still count against the per-IP cap.
        login_throttle.check(f"register:{number}", ip)
    except login_throttle.TooManyAttempts as e:
        raise AuthFlowError(str(e), 429, retry_after_sec=e.retry_after_sec) from None

    try:
        user = admin_service.create_app_user(whatsapp_number=number, app_password=app_password)
    except admin_service.WhatsappNumberTaken:
        raise AuthFlowError(
            "That WhatsApp number is already registered. Log in instead — or, if "
            "you registered before app passwords existed, fill in your Angel One "
            "details below to set one.",
            409,
            field="whatsapp_number",
        ) from None
    except ValueError as e:
        raise AuthFlowError(str(e), 400, field="app_password") from None
    except Exception:
        logger.exception("Registration failed during the database write")
        raise AuthFlowError("Registration failed. Please try again.", 500) from None

    end_web_session(request)
    bind_user(request, user.id)
    logger.info("registration complete for user id=%s (no broker)", user.id)
    return admin_service.public_view(user)


def logout(request: Request) -> None:
    """End the broker session and revoke every copy of this user's cookie.

    The cookie is signed but stateless, so clearing it here does nothing about
    a copy saved earlier. Bumping the user's ``session_version`` does: every
    cookie issued before now carries the old version and is refused from the
    next request on — on this device and any other. The live broker session,
    its ADK runners and its caches are released too.
    """
    uid = request.session.get("uid")
    if uid:
        try:
            admin_service.bump_session_version(int(uid))
        except Exception:
            # Still end this device's session; say so loudly, since saved copies
            # of the cookie stay valid until the bump succeeds.
            logger.exception("Logout: could not bump session version for uid=%s", uid)
    end_web_session(request)
