"""Request-scoped helpers shared by every router.

Session lookup, the login guard, template context and premium-user resolution
live here so no router has to reach into another router for them.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import Request
from sqlmodel import select

from adminApi import service as admin_service
from db import get_session
from db.models import User
from session_manager import sessions

logger = logging.getLogger(__name__)

# Starlette session keys: separate ADK chat ids for finance vs trading agent.
ADK_CHAT_SESSION_KEY = "adk_chat_session_id"


ADK_TRADING_CHAT_SESSION_KEY = "adk_trading_chat_session_id"



def session_id(request: Request) -> str | None:
    return request.session.get("sid")



def start_session(request: Request, creds) -> tuple[str, object]:
    """Build a broker session from ``creds`` and attach it to the cookie.

    Writes two keys. ``sid`` points at the in-memory ``AngelOneClient``; ``uid``
    records which ``users`` row it came from, which is what lets
    :func:`current_client` rebuild the session later without a password. ``uid``
    is only written when the row id is known — enrollment calls this before the
    row exists and sets it afterwards.

    Raises whatever ``AngelOneClient`` raises if the broker rejects the
    credentials, so callers can show that to the user.
    """
    sid = uuid.uuid4().hex
    client = sessions.create_session(
        sid, creds.api_key, creds.client_id, creds.password, creds.totp_secret
    )
    request.session["sid"] = sid
    if getattr(creds, "user_id", 0):
        request.session["uid"] = creds.user_id
    return sid, client



def current_client(request: Request):
    """The live broker client for this request, rebuilding it if it has expired.

    This is what removes the re-typing. ``session_manager`` keeps credentials in
    memory for 8 hours and loses them on restart, so before enrollment existed
    either event meant another four-field form. Now the signed cookie still says
    which user this is, and the credentials are in the database, so the session
    can be rebuilt silently.

    The security trade is explicit: within the cookie's lifetime, the cookie
    alone is enough to trade. ``SESSION_COOKIE_MAX_AGE_SEC`` in :mod:`web.app`
    is what bounds that, and logging out clears ``uid``.
    """
    sid = session_id(request)
    if sid:
        client = sessions.get_client(sid)
        if client:
            return client

    uid = request.session.get("uid")
    if not uid:
        return None

    try:
        creds = admin_service.credentials_for_user(int(uid))
    except Exception:
        logger.exception("Session rebuild: could not read credentials for uid=%s", uid)
        creds = None

    if creds is None:
        # Row gone, deactivated, or undecryptable. Drop ``uid`` so this does not
        # repeat on every page load.
        request.session.pop("uid", None)
        return None

    try:
        _sid, client = start_session(request, creds)
    except Exception:
        # Angel One is down or the stored credentials have gone stale. Dropping
        # ``uid`` matters more than it looks: without it, every page load would
        # retry a login that sleeps through 5 attempts before giving up.
        logger.exception("Session rebuild: Angel One login failed for uid=%s", uid)
        request.session.pop("uid", None)
        return None

    logger.info("Session rebuilt from cookie for user id=%s", uid)
    return client



def ensure_adk_chat_session_id(request: Request, key: str = ADK_CHAT_SESSION_KEY) -> str:
    raw = request.session.get(key)
    if not raw:
        raw = uuid.uuid4().hex
        request.session[key] = raw
    return str(raw)



def template_context(request: Request, active: str = "") -> dict:
    return {
        "request": request,
        "logged_in": current_client(request) is not None,
        "active": active,
    }



def registered_user_for_session(client) -> User | None:
    """Look up the persisted ``User`` row matching the live broker session's
    ``angel_client_id``. Returns ``None`` if not registered (or DB unreachable)."""
    if client is None:
        return None
    try:
        with get_session() as s:
            return s.exec(
                select(User).where(User.angel_client_id == client.client_id)
            ).first()
    except Exception:
        logger.exception("Premium status lookup failed")
        return None



def client_ip(request: Request) -> str:
    """Best-effort client IP, honouring a single proxy hop (Render/X-Forwarded-For)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"



# ── Authenticated routes ───────────────────────────────────────────────────


def require_login(request: Request):
    """The broker client for a logged-in request, or ``None``.

    Goes through :func:`current_client`, so a user whose 8-hour broker session
    lapsed mid-visit is reconnected instead of being bounced to the login form.
    """
    return current_client(request)



def premium_user(request: Request) -> User | None:
    """Return the active registered User for the current session, or None."""
    client = current_client(request)
    if client is None:
        return None
    user = registered_user_for_session(client)
    return user if (user and user.is_active) else None
