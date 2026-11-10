"""Request-scoped helpers shared by every router.

Session lookup, the login guards, template context and premium-user resolution
live here so no router has to reach into another router for them.

Two levels of access
--------------------
- **App user** (:func:`current_app_user`, :func:`require_app_user`): a signed
  cookie whose session version still matches an active row. Enough for the
  account, session and brokerage-connection endpoints. No broker is involved.
- **Brokerage** (:func:`current_client`, :func:`require_brokerage`): an app user
  whose ``brokerage_status`` is ``connected``, plus a live Angel One session
  built from the stored credentials. Every endpoint that shows portfolio, market
  or research data needs this.

A user without a working broker link is still signed in: they get a 403 that
says what to do, never a 401 that would sign them out.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

from fastapi import Request
from sqlmodel import select

from adminApi import service as admin_service
from angel_client import BrokerAuthRejected
from db import get_session
from db.models import BrokerageStatus, User
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



class BrokerageAccessError(Exception):
    """Why a request may not use brokerage features, as an HTTP answer.

    Raised by :func:`require_brokerage` and turned into JSON by the handler in
    :mod:`web.app`: ``{"error", "detail", "code"}``. ``error`` is what the SPA
    reads, ``detail`` mirrors FastAPI's own errors, and ``code`` lets a client
    tell "connect a broker" from "reconnect it" from "try again later".
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _not_authenticated() -> BrokerageAccessError:
    # A fresh instance per raise: a shared one would accumulate tracebacks.
    return BrokerageAccessError(401, "not_authenticated", "Not authenticated")

CONNECT_BROKERAGE_MESSAGE = "Please connect your brokerage account to access this feature."


def _brokerage_error(status: str) -> BrokerageAccessError:
    if status in (BrokerageStatus.EXPIRED, BrokerageStatus.FAILED, BrokerageStatus.DISCONNECTED):
        return BrokerageAccessError(
            403,
            "brokerage_reauth_required",
            "Your brokerage connection needs attention. Reconnect your Angel One "
            "account to access this feature.",
        )
    return BrokerageAccessError(403, "brokerage_not_connected", CONNECT_BROKERAGE_MESSAGE)


def _drop_broker_session(request: Request) -> None:
    """Release this request's broker session but keep the user signed in."""
    from web.session_cleanup import release_session_resources

    sid = request.session.pop("sid", None)
    if sid:
        release_session_resources(sid)
        sessions.remove_session(sid)


def current_client(request: Request):
    """The live broker client for this request, rebuilding it if it has expired.

    ``None`` when the request may not use the broker. Callers that need to say
    *why* use :func:`require_brokerage` instead.

    Every call re-checks the cookie against the database (:func:`_verified_state`):
    a logged-out (version bumped), deactivated or deleted user gets ``None``
    here even if their in-memory broker session is still alive, and that
    session is torn down. A signed-in user whose broker is not ``connected``
    also gets ``None`` — but stays signed in: only their broker session goes.
    The live client must also belong to the user the cookie names.

    If the cookie is valid but the 8-hour broker session lapsed (or the server
    restarted), the session is rebuilt from the stored credentials, once per
    user however many requests arrive together. The security trade is explicit:
    within the cookie's lifetime, the cookie alone is enough to read the
    account. ``SESSION_COOKIE_MAX_AGE_SEC`` in :mod:`web.app` bounds that, and
    logging out revokes every copy of the cookie.

    A rebuild that Angel One *rejects* marks the link ``expired`` so the user is
    asked to reconnect; one that fails for any other reason (the broker is down)
    changes nothing and is reported as unavailable.
    """
    try:
        return _client_or_error(request)
    except BrokerageAccessError:
        return None


def _client_or_error(request: Request):
    state = _verified_state(request)
    if state is None:
        if request.session.get("sid") or request.session.get("uid"):
            end_web_session(request)
        raise _not_authenticated()

    if state.brokerage_status != BrokerageStatus.CONNECTED or not state.angel_client_id:
        _drop_broker_session(request)
        raise _brokerage_error(state.brokerage_status)

    sid = session_id(request)
    if sid:
        client = sessions.get_client(sid)
        if client is not None:
            if client.client_id != state.angel_client_id:
                # Normal after a reconnect to a different account; never serve
                # the old account's session for the new link.
                logger.warning("Session %s does not match user id=%s's broker link; dropping it",
                               sid[:8], state.user_id)
                _drop_broker_session(request)
            else:
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
            raise BrokerageAccessError(
                503, "brokerage_unavailable",
                "Your stored brokerage credentials could not be read. Please try again later.",
            ) from None

        if creds is None:
            # Raced with a disconnect, or the row lost its link.
            raise _brokerage_error(BrokerageStatus.NOT_CONNECTED)

        try:
            new_sid, client = start_session(request, creds)
        except BrokerAuthRejected:
            # Angel One refused the stored credentials (PIN or TOTP changed). Stop
            # using them until the user reconnects; without this, every page load
            # would retry a login that sleeps through 5 attempts before failing.
            logger.warning("Session rebuild: Angel One rejected stored credentials for uid=%s",
                           state.user_id)
            admin_service.set_brokerage_status(
                state.user_id, BrokerageStatus.EXPIRED, only_if=(BrokerageStatus.CONNECTED,)
            )
            raise _brokerage_error(BrokerageStatus.EXPIRED) from None
        except Exception:
            logger.exception("Session rebuild: Angel One login failed for uid=%s", state.user_id)
            raise BrokerageAccessError(
                503, "brokerage_unavailable",
                "Angel One could not be reached. Please try again in a few minutes.",
            ) from None

        with _recent_rebuilds_lock:
            _recent_rebuilds[state.user_id] = (new_sid, time.time())

    logger.info("Session rebuilt from cookie for user id=%s", state.user_id)
    return client



def current_app_user(request: Request) -> User | None:
    """The signed-in, active ``User`` for this request, broker or not; else None.

    Identity comes from the verified cookie (``uid`` plus a matching session
    version) — never from request parameters. No broker session is built or
    required, so this is cheap and works for users who never connected one.
    """
    state = _verified_state(request)
    if state is None:
        if request.session.get("sid") or request.session.get("uid"):
            end_web_session(request)
        return None
    try:
        with get_session() as s:
            user = s.get(User, state.user_id)
    except Exception:
        logger.exception("User lookup failed for uid=%s", state.user_id)
        return None
    return user if (user and user.is_active) else None


def current_user(request: Request) -> User | None:
    """The signed-in, active, broker-connected ``User`` for this request, or None.

    Identity comes from the verified cookie (``uid`` plus a matching session
    version) — never from request parameters — and the live broker session must
    belong to the same user. This is what every brokerage resource is keyed on;
    for anything a user without a broker may do, use :func:`current_app_user`.
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
        "logged_in": current_app_user(request) is not None,
        "active": active,
    }


#: Where a signed-in user without a usable broker link is sent from a
#: server-rendered page. The React app is served at ``web.app.SPA_MOUNT_PATH``.
CONNECT_BROKERAGE_PAGE = "/app/connect-brokerage"


def page_redirect(request: Request):
    """Where a server-rendered page sends someone who may not see it.

    Signed out goes to the login form; signed in without a connected broker
    goes to connect one — not back to login, which would loop.
    """
    from fastapi.responses import RedirectResponse

    if current_app_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse(CONNECT_BROKERAGE_PAGE, status_code=302)



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
    """The broker client for a logged-in, connected request, or ``None``.

    Kept for the server-rendered pages, which redirect rather than answer JSON.
    API endpoints use :func:`require_brokerage`, which says why access failed.
    """
    return current_client(request)


def require_app_user(request: Request) -> User:
    """FastAPI dependency: the signed-in user, or a 401. No broker needed."""
    user = current_app_user(request)
    if user is None:
        raise _not_authenticated()
    return user


def require_brokerage(request: Request):
    """FastAPI dependency: the live broker client, or the reason there is none.

    401 when not signed in; 403 ``brokerage_not_connected`` or
    ``brokerage_reauth_required`` when signed in without a usable broker link;
    503 ``brokerage_unavailable`` when Angel One cannot be reached. Use it as
    ``client = Depends(require_brokerage)`` on every endpoint that touches
    portfolio, market or research data.
    """
    return _client_or_error(request)



def premium_user(request: Request) -> User | None:
    """Return the active registered User for the current session, or None."""
    return current_user(request)
