"""Request-scoped helpers shared by every router.

Session lookup, the login guard, template context and premium-user resolution
live here so no router has to reach into another router for them.
"""
from __future__ import annotations

import logging
import uuid
from functools import partial

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


_NEEDS_REBUILD = object()
_CACHE = "_broker_client"


def _resolve_without_login(request: Request):
    """``(client | None | _NEEDS_REBUILD, state)``. Never logs in.

    Re-checks the cookie against the database on every call: a logged-out
    (version bumped), deactivated or deleted user gets ``None`` even if their
    in-memory broker session is still alive, and that session is torn down.
    The live client must also belong to the user the cookie names.
    """
    state = _verified_state(request)
    if state is None:
        if request.session.get("sid") or request.session.get("uid"):
            end_web_session(request)
        return None, None
    sid = session_id(request)
    if sid:
        client = sessions.get_client(sid)
        if client is not None:
            if client.client_id != state.angel_client_id:
                logger.error("Session %s does not belong to user id=%s; ending it",
                             sid[:8], state.user_id)
                end_web_session(request)
                return None, state
            return client, state
    return _NEEDS_REBUILD, state


async def broker_client(request: Request):
    """The live broker client for this request, rebuilding it if it has expired.

    Every async handler that needs the signed-in user calls this first; the
    result is cached on ``request.state`` (one dict per request), so
    :func:`current_user`, :func:`template_context` and the like reuse it.

    If the cookie is valid but the 8-hour broker session lapsed (or the server
    restarted), the session is rebuilt from the stored credentials in a worker
    thread, once per user however many requests arrive together
    (``web.broker_sessions``). The security trade is explicit: within the
    cookie's lifetime, the cookie alone is enough to read the account.
    ``SESSION_COOKIE_MAX_AGE_SEC`` in :mod:`web.app` bounds that, and logging out
    revokes every copy of the cookie.
    """
    if hasattr(request.state, _CACHE):
        return getattr(request.state, _CACHE)
    client = await _resolve(request)
    setattr(request.state, _CACHE, client)
    return client


async def _resolve(request: Request):
    from web import broker_sessions

    found, state = _resolve_without_login(request)
    if found is not _NEEDS_REBUILD:
        return found
    try:
        runtime = broker_sessions.get_runtime()
    except broker_sessions.BrokerRuntimeUnavailable:
        logger.error("Session rebuild: broker runtime unavailable for uid=%s", state.user_id)
        return None

    old_sid = session_id(request)
    recalled = runtime.recall(state.user_id, state.angel_client_id, state.session_version)
    if recalled is None:
        try:
            result = await runtime.login(
                f"user:{state.user_id}",
                partial(admin_service.credentials_for_user, state.user_id),
            )
        except Exception:
            # Angel One is down or the stored credentials have gone stale. Ending
            # the session matters: without it every page load would retry.
            logger.exception("Session rebuild: Angel One login failed for uid=%s", state.user_id)
            end_web_session(request)
            return None
        if result is None:  # no stored credentials
            end_web_session(request)
            return None
        new_sid, client = result
        runtime.remember(state.user_id, new_sid, client.client_id, state.session_version)
        logger.info("Session rebuilt from cookie for user id=%s", state.user_id)
    else:
        new_sid, client = recalled

    request.session["sid"] = new_sid
    if old_sid and old_sid != new_sid:
        # The old session is gone; its runners, feed and caches are released
        # by the next cleanup sweep, on the event loop.
        sessions.mark_for_release(old_sid)
    return client


def current_client(request: Request):
    """The broker client this request resolved with :func:`broker_client`.

    Never logs in and never blocks: an async handler must ``await
    broker_client(request)`` first (``tests/test_app_wiring.py`` checks that).
    Called without it, a session that needs rebuilding reads as signed out.
    """
    if hasattr(request.state, _CACHE):
        return getattr(request.state, _CACHE)
    found, _state = _resolve_without_login(request)
    if found is _NEEDS_REBUILD:
        logger.warning("current_client called before broker_client; not rebuilding")
        return None
    return found


def sign_out_everywhere(request: Request, user_id: int) -> None:
    """Drop every web broker session this user's account holds, on all devices.

    Used by logout and account deletion. The session-version bump (done by the
    caller) is what makes old cookies invalid; this releases the memory and
    per-session resources now instead of at expiry. MCP sessions are separate.
    """
    from web import broker_sessions
    from web.session_cleanup import release_session_resources

    try:
        state = admin_service.session_state(int(user_id))
    except Exception:
        logger.exception("Sign-out: could not read state for uid=%s", user_id)
        state = None
    if state is not None:
        for sid in sessions.remove_web_sessions_for(state.angel_client_id):
            release_session_resources(sid)
    try:
        broker_sessions.get_runtime().forget_user(int(user_id))
    except broker_sessions.BrokerRuntimeUnavailable:
        pass


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
    """Best-effort, forgeable client IP, for abuse limits only. See :mod:`web.client_ip`."""
    from web.client_ip import abuse_client_ip

    return abuse_client_ip(request.headers, request.client.host if request.client else None)


def trusted_ip(request: Request) -> str | None:
    """The client IP our proxies vouch for, or ``None`` until that is verified.

    For security decisions. ``None`` means "skip the per-IP check", never "deny".
    """
    from web.client_ip import trusted_client_ip

    return trusted_client_ip(request.headers, request.client.host if request.client else None)



# ── Authenticated routes ───────────────────────────────────────────────────


def require_login(request: Request):
    """Deprecated alias of :func:`current_client`; async handlers await :func:`broker_client`."""
    return current_client(request)



def premium_user(request: Request) -> User | None:
    """Return the active registered User for the current session, or None."""
    return current_user(request)
