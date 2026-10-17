"""Request-scoped helpers shared by every router.

Session lookup, the login guard, template context and premium-user resolution
live here so no router has to reach into another router for them.
"""
from __future__ import annotations

import logging
import threading
import time
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

    Writes ``sid`` (points at the in-memory ``AngelOneClient``), and — when the
    ``users`` row is known — ``uid`` and ``uv``, the row's current
    ``session_version``. :func:`current_client` honours the cookie only while
    ``uv`` still matches, which is what lets logout revoke saved copies of this
    stateless cookie. Enrollment calls this before the row exists and sets
    ``uid``/``uv`` itself afterwards (:func:`bind_user`).

    Raises whatever ``AngelOneClient`` raises if the broker rejects the
    credentials, so callers can show that to the user.
    """
    sid = uuid.uuid4().hex
    client = sessions.create_session(
        sid, creds.api_key, creds.client_id, creds.password, creds.totp_secret
    )
    request.session["sid"] = sid
    if getattr(creds, "user_id", 0):
        bind_user(request, creds.user_id)
    return sid, client



def bind_user(request: Request, user_id: int) -> None:
    """Record ``user_id`` and its current session version in the cookie."""
    state = admin_service.session_state(int(user_id))
    request.session["uid"] = int(user_id)
    request.session["uv"] = state.session_version if state else None



def end_web_session(request: Request) -> None:
    """Drop this request's broker session, its per-session state, and the cookie."""
    from web.session_cleanup import release_session_resources

    sid = session_id(request)
    if sid:
        release_session_resources(sid)
        sessions.remove_session(sid)
    request.session.clear()



# Per-process memo of the session a user's cookie was just rebuilt into, so
# several requests arriving together with the same expired cookie share one
# broker login instead of each starting their own.
_REBUILD_REUSE_SEC = 30.0
_recent_rebuilds: dict[int, tuple[str, float]] = {}
_recent_rebuilds_lock = threading.Lock()



def _verified_state(request: Request):
    """The cookie's user, if the cookie is still valid for them, else None.

    Valid means: a ``uid`` is present, the row exists and is active, and the
    cookie's ``uv`` equals the row's current ``session_version``. A database
    error denies.
    """
    uid = request.session.get("uid")
    if not uid:
        return None
    try:
        state = admin_service.session_state(int(uid))
    except Exception:
        logger.exception("Session check failed for uid=%s; denying", uid)
        return None
    if state is None or not state.is_active:
        return None
    if request.session.get("uv") != state.session_version:
        return None
    return state



def current_client(request: Request):
    """The live broker client for this request, rebuilding it if it has expired.

    Every call re-checks the cookie against the database (:func:`_verified_state`):
    a logged-out (version bumped), deactivated or deleted user gets ``None``
    here even if their in-memory broker session is still alive, and that
    session is torn down. The live client must also belong to the user the
    cookie names.

    If the cookie is valid but the 8-hour broker session lapsed (or the server
    restarted), the session is rebuilt from the stored credentials, once per
    user however many requests arrive together. The security trade is explicit:
    within the cookie's lifetime, the cookie alone is enough to read the
    account. ``SESSION_COOKIE_MAX_AGE_SEC`` in :mod:`web.app` bounds that, and
    logging out revokes every copy of the cookie.
    """
    state = _verified_state(request)
    if state is None:
        if request.session.get("sid") or request.session.get("uid"):
            end_web_session(request)
        return None

    sid = session_id(request)
    if sid:
        client = sessions.get_client(sid)
        if client is not None:
            if client.client_id != state.angel_client_id:
                logger.error("Session %s does not belong to user id=%s; ending it",
                             sid[:8], state.user_id)
                end_web_session(request)
                return None
            return client

    with sessions.key_lock(f"web-rebuild-{state.user_id}"):
        with _recent_rebuilds_lock:
            recent = _recent_rebuilds.get(state.user_id)
        if recent and time.time() - recent[1] < _REBUILD_REUSE_SEC:
            client = sessions.get_client(recent[0])
            if client is not None and client.client_id == state.angel_client_id:
                request.session["sid"] = recent[0]
                return client

        try:
            creds = admin_service.credentials_for_user(state.user_id)
        except Exception:
            logger.exception("Session rebuild: could not read credentials for uid=%s", state.user_id)
            creds = None

        if creds is None:
            end_web_session(request)
            return None

        try:
            new_sid, client = start_session(request, creds)
        except Exception:
            # Angel One is down or the stored credentials have gone stale. Ending
            # the session matters more than it looks: without it, every page load
            # would retry a login that sleeps through 5 attempts before giving up.
            logger.exception("Session rebuild: Angel One login failed for uid=%s", state.user_id)
            end_web_session(request)
            return None

        with _recent_rebuilds_lock:
            _recent_rebuilds[state.user_id] = (new_sid, time.time())

    logger.info("Session rebuilt from cookie for user id=%s", state.user_id)
    return client



def current_user(request: Request) -> User | None:
    """The signed-in, active ``User`` for this request, or None.

    Identity comes from the verified cookie (``uid`` plus a matching session
    version) — never from request parameters — and the live broker session must
    belong to the same user. This is what every user-owned resource is keyed on.
    """
    if current_client(request) is None:
        return None
    uid = request.session.get("uid")
    try:
        with get_session() as s:
            user = s.get(User, int(uid))
    except Exception:
        logger.exception("User lookup failed for uid=%s", uid)
        return None
    return user if (user and user.is_active) else None



def current_user_id(request: Request) -> int | None:
    user = current_user(request)
    return user.id if user else None



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
    """Deprecated lookup by broker ``client_id``. Prefer :func:`current_user`.

    Kept for non-request callers; request handlers must use :func:`current_user`,
    which ties the user to the verified cookie rather than to the broker client.
    """
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
    return current_user(request)
