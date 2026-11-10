"""Connecting, checking and disconnecting a user's Angel One account.

Separate from signing in: a user registers with a phone number and password
(:mod:`web.routers.auth_api`), and links a broker here whenever they choose.
Everything is keyed on the signed-in user from the verified cookie — a user id
in a request body is never read.

The order in :func:`api_brokerage_register` is the point of this module:
credentials are proved with Angel One *first*, and only a successful login is
written, in one transaction. A failed attempt therefore never overwrites a link
that works, and never marks one connected.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from adminApi import service as admin_service
from angel_client import BrokerAuthRejected
from db.models import BrokerageStatus, User
from services import login_throttle
from session_manager import sessions
from web.dependencies import client_ip, require_app_user

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}

# Statuses a failed attempt must not overwrite: ``connected`` because the
# stored link still works, ``expired`` because it records that it stopped.
_KEEP_ON_FAILURE = (BrokerageStatus.CONNECTED, BrokerageStatus.EXPIRED)


def _status_body(user: User) -> dict:
    return {
        "brokerage_status": user.brokerage_status,
        "is_connected": admin_service.is_connected(user),
        "provider": admin_service.brokerage_provider(user),
    }


def _error(message: str, status_code: int, field: str | None = None, **extra) -> JSONResponse:
    body: dict = {"error": message, "detail": message, **extra}
    if field:
        body["field"] = field
    return JSONResponse(body, status_code=status_code, headers=_NO_STORE)


def _mcp_session_key(user_id: int) -> str:
    from services import mcp_tokens

    return mcp_tokens.mcp_session_key(user_id)


def _swap_web_session(request: Request, new_sid: str) -> None:
    """Point the cookie at ``new_sid``, releasing whatever it pointed at before."""
    from web.session_cleanup import release_session_resources

    old = request.session.get("sid")
    if old and old != new_sid:
        release_session_resources(old)
        sessions.remove_session(old)
    request.session["sid"] = new_sid


@router.get("/api/brokerage/status")
async def api_brokerage_status(user: User = Depends(require_app_user)):
    """The signed-in user's broker link. Never includes a credential or token."""
    return JSONResponse(_status_body(user), headers=_NO_STORE)


@router.post("/api/brokerage/register")
async def api_brokerage_register(request: Request, user: User = Depends(require_app_user)):
    """Prove Angel One credentials, then link them to the signed-in user.

    Body: ``client_id``, ``api_key``, ``password`` (the Angel One PIN) and
    ``totp_secret`` — the base32 secret behind the authenticator app, not a
    six-digit code. The secret is what lets the server open a fresh broker
    session every eight hours without asking the user again.
    """
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _error("Invalid JSON body", 400)

    def text(key: str, strip: bool = True) -> str:
        value = body.get(key)
        if not isinstance(value, str):
            return ""
        return value.strip() if strip else value

    # Stored exactly as typed (trimmed), as enrollment always has: changing the
    # case here would let one account dodge the uniqueness check on old rows.
    client_id = text("client_id")
    api_key = text("api_key")
    password = text("password", strip=False)
    totp_secret = text("totp_secret").replace(" ", "")
    for name, value in (("client_id", client_id), ("api_key", api_key),
                        ("password", password), ("totp_secret", totp_secret)):
        if not value:
            return _error("Fill in every Angel One field.", 400, field=name)

    ip = client_ip(request)
    try:
        # Each attempt costs an Angel One login (up to five tries); cap them.
        login_throttle.check(f"brokerage:{user.id}", ip)
    except login_throttle.TooManyAttempts as e:
        return JSONResponse(
            {"error": str(e), "detail": str(e)},
            status_code=429,
            headers={**_NO_STORE, "Retry-After": str(e.retry_after_sec)},
        )

    sid = uuid.uuid4().hex
    try:
        client = await asyncio.to_thread(
            sessions.create_session, sid, api_key, client_id, password, totp_secret
        )
    except Exception as e:
        # Nothing is written but the status, and not even that over a link that
        # works (or one recorded as expired). Never log the exception text: it
        # can quote what the broker was sent.
        sessions.remove_session(sid)
        rejected = isinstance(e, BrokerAuthRejected)
        logger.warning(
            "brokerage register: Angel One login %s for user id=%s",
            "rejected" if rejected else f"failed ({type(e).__name__})",
            user.id,
        )
        # Only a rejection says anything about the credentials; an outage
        # leaves the status alone.
        changed = rejected and admin_service.set_brokerage_status(
            user.id,
            BrokerageStatus.FAILED,
            only_if=tuple(s for s in BrokerageStatus if s not in _KEEP_ON_FAILURE),
        )
        current = admin_service.session_state(user.id)
        return _error(
            "Angel One did not accept these credentials. Check the client ID, API key, "
            "PIN and TOTP secret and try again."
            if rejected
            else "Could not reach Angel One to verify these credentials. Please try again.",
            400 if rejected else 502,
            code="brokerage_auth_failed" if rejected else "brokerage_unavailable",
            brokerage_status=current.brokerage_status if current else None,
            status_changed=changed,
        )

    try:
        linked = admin_service.connect_brokerage(
            user.id,
            api_key=api_key,
            client_id=client.client_id,
            password=password,
            totp_secret=totp_secret,
        )
    except admin_service.BrokerAccountInUse:
        sessions.remove_session(sid)
        return _error(
            "That Angel One account is already linked to another user. If you "
            "registered it before app passwords existed, log in with that number instead.",
            409,
            field="client_id",
        )
    except admin_service.UserNotFound:
        sessions.remove_session(sid)
        return _error("Not authenticated", 401)
    except Exception:
        sessions.remove_session(sid)
        logger.exception("brokerage register: storing the link failed for user id=%s", user.id)
        return _error("Could not save your brokerage connection. Please try again.", 500)

    # The MCP endpoint keeps its own session per user; drop it so its next call
    # is built from the link just stored rather than the previous one.
    sessions.remove_session(_mcp_session_key(user.id))
    _swap_web_session(request, sid)
    login_throttle.clear(f"brokerage:{user.id}", ip)
    logger.info("brokerage register: user id=%s connected", user.id)
    return JSONResponse(
        {"ok": True, **_status_body(linked), "available_features": admin_service.available_features(linked)},
        headers=_NO_STORE,
    )


@router.post("/api/brokerage/disconnect")
async def api_brokerage_disconnect(request: Request, user: User = Depends(require_app_user)):
    """Unlink Angel One: stored credentials are wiped, live sessions ended.

    The user stays signed in to the app and keeps every non-broker feature.
    """
    from web.session_cleanup import release_session_resources

    try:
        unlinked = admin_service.disconnect_brokerage(user.id)
    except admin_service.UserNotFound:
        return _error("Not authenticated", 401)

    sid = request.session.pop("sid", None)
    if sid:
        release_session_resources(sid)
        sessions.remove_session(sid)
    sessions.remove_session(_mcp_session_key(user.id))
    return JSONResponse(
        {"ok": True, **_status_body(unlinked), "available_features": admin_service.available_features(unlinked)},
        headers=_NO_STORE,
    )
