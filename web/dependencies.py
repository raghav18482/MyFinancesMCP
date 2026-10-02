"""Request-scoped helpers shared by every router.

Session lookup, the login guard, template context and premium-user resolution
live here so no router has to reach into another router for them.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import Request
from sqlmodel import select

from db import get_session
from db.models import User
from session_manager import sessions

logger = logging.getLogger(__name__)

# Starlette session keys: separate ADK chat ids for finance vs trading agent.
ADK_CHAT_SESSION_KEY = "adk_chat_session_id"


ADK_TRADING_CHAT_SESSION_KEY = "adk_trading_chat_session_id"



def session_id(request: Request) -> str | None:
    return request.session.get("sid")



def ensure_adk_chat_session_id(request: Request, key: str = ADK_CHAT_SESSION_KEY) -> str:
    raw = request.session.get(key)
    if not raw:
        raw = uuid.uuid4().hex
        request.session[key] = raw
    return str(raw)



def template_context(request: Request, active: str = "") -> dict:
    return {
        "request": request,
        "logged_in": session_id(request) is not None and sessions.get_client(session_id(request)) is not None,
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
    sid = session_id(request)
    if not sid:
        return None
    return sessions.get_client(sid)



def premium_user(request: Request) -> User | None:
    """Return the active registered User for the current session, or None."""
    sid = session_id(request)
    client = sessions.get_client(sid) if sid else None
    if client is None:
        return None
    user = registered_user_for_session(client)
    return user if (user and user.is_active) else None
