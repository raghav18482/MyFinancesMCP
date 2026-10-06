"""Agent chat threads and the conversation endpoint, including inline
trade approval commands."""
from __future__ import annotations

import re
import uuid
import logging
from datetime import datetime

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from sqlmodel import select

from db import get_session
from db.models import ChatThread

from services.trade_proposals import proposal_store, execute_proposal

from session_manager import sessions
from services.adk_runner_registry import app_name_for, registry

from db.models import utcnow
from web.dependencies import (
    ADK_CHAT_SESSION_KEY,
    ADK_TRADING_CHAT_SESSION_KEY,
    ensure_adk_chat_session_id,
    premium_user,
    session_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_APPROVE_RE = re.compile(r"^APPROVE\s+([a-f0-9]{8,16})$", re.IGNORECASE)


_REJECT_RE = re.compile(r"^REJECT\s+([a-f0-9]{8,16})$", re.IGNORECASE)



# ── Agent chat persistence (conversation threads) ──────────────────────────

_AGENT_TYPES = ("finance", "trading")



def _normalize_agent_type(raw: str | None) -> str:
    at = (raw or "finance").strip().lower()
    return at if at in _AGENT_TYPES else "finance"



def _thread_to_dict(t: ChatThread) -> dict:
    return {
        "id": t.id,
        "agent_type": t.agent_type,
        "title": t.title,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }



def _derive_title(message: str) -> str:
    """Build a short conversation title from the first user message."""
    text = " ".join((message or "").split())
    if len(text) > 60:
        text = text[:57].rstrip() + "…"
    return text or "New conversation"



def _touch_thread(thread_id: int, first_message: str) -> None:
    """Bump updated_at and set the title from the first message if still default."""
    try:
        with get_session() as db:
            t = db.get(ChatThread, thread_id)
            if not t:
                return
            if t.title == "New conversation" and first_message.strip():
                t.title = _derive_title(first_message)
            t.updated_at = utcnow()
            db.add(t)
            db.commit()
    except Exception:
        logger.warning("Failed to touch chat thread %s", thread_id)



@router.get("/api/agent/threads")
async def api_agent_threads_list(request: Request, agent_type: str = Query("")):
    """List the current premium user's conversations for the sidebar."""
    sid = session_id(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = premium_user(request)
    if not user:
        return JSONResponse({"premium": False, "threads": []})
    with get_session() as db:
        stmt = select(ChatThread).where(
            ChatThread.user_id == user.id,
            ChatThread.archived == False,  # noqa: E712
        )
        if agent_type.strip():
            stmt = stmt.where(ChatThread.agent_type == _normalize_agent_type(agent_type))
        stmt = stmt.order_by(ChatThread.updated_at.desc())
        threads = db.exec(stmt).all()
    return JSONResponse({"premium": True, "threads": [_thread_to_dict(t) for t in threads]})



@router.post("/api/agent/threads")
async def api_agent_threads_create(request: Request):
    """Create a fresh conversation thread for a premium user."""
    sid = session_id(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = premium_user(request)
    if not user:
        return JSONResponse(
            {"error": "Saved conversations are available for premium users."},
            status_code=403,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    agent_type = _normalize_agent_type(body.get("agent_type"))
    thread = ChatThread(
        user_id=user.id,
        agent_type=agent_type,
        adk_session_id=uuid.uuid4().hex,
        title="New conversation",
    )
    with get_session() as db:
        db.add(thread)
        db.commit()
        db.refresh(thread)
    return JSONResponse({"ok": True, "thread": _thread_to_dict(thread)})



@router.get("/api/agent/threads/{thread_id}/messages")
async def api_agent_thread_messages(request: Request, thread_id: int):
    """Return the persisted {role, text} messages for one conversation."""
    sid = session_id(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = premium_user(request)
    if not user:
        return JSONResponse({"messages": []})
    with get_session() as db:
        thread = db.get(ChatThread, thread_id)
        if not thread or thread.user_id != user.id:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)
        agent_type = thread.agent_type
        adk_session_id = thread.adk_session_id
    try:
        messages = await registry.get_messages(
            user_id=f"user-{user.id}",
            adk_session_id=adk_session_id,
            app_name=app_name_for(agent_type),
        )
    except Exception:
        logger.exception("Failed to load messages for thread %s", thread_id)
        messages = []
    return JSONResponse({"messages": messages, "agent_type": agent_type})



@router.patch("/api/agent/threads/{thread_id}")
async def api_agent_thread_rename(request: Request, thread_id: int):
    """Rename a conversation thread."""
    sid = session_id(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = premium_user(request)
    if not user:
        return JSONResponse({"error": "Not authorized"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    title = " ".join((body.get("title") or "").split())[:120].strip()
    if not title:
        return JSONResponse({"error": "Title cannot be empty"}, status_code=400)
    with get_session() as db:
        thread = db.get(ChatThread, thread_id)
        if not thread or thread.user_id != user.id:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)
        thread.title = title
        thread.updated_at = utcnow()
        db.add(thread)
        db.commit()
        db.refresh(thread)
        return JSONResponse({"ok": True, "thread": _thread_to_dict(thread)})



@router.delete("/api/agent/threads/{thread_id}")
async def api_agent_thread_delete(request: Request, thread_id: int):
    """Delete a conversation thread and its persisted ADK session."""
    sid = session_id(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = premium_user(request)
    if not user:
        return JSONResponse({"error": "Not authorized"}, status_code=403)
    with get_session() as db:
        thread = db.get(ChatThread, thread_id)
        if not thread or thread.user_id != user.id:
            return JSONResponse({"ok": True})
        agent_type = thread.agent_type
        adk_session_id = thread.adk_session_id
        db.delete(thread)
        db.commit()
    try:
        await registry.delete_session(
            user_id=f"user-{user.id}",
            adk_session_id=adk_session_id,
            app_name=app_name_for(agent_type),
        )
    except Exception:
        logger.warning("Failed to delete ADK session for thread %s", thread_id)
    return JSONResponse({"ok": True})



@router.post("/api/agent/chat")
async def api_agent_chat(request: Request):
    """Run an ADK agent (finance or trading). Uses Angel session from web login.

    Premium users that pass a ``thread_id`` get a DB-persisted conversation;
    everyone else falls back to the cookie-scoped ephemeral session.
    """
    sid = session_id(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    agent_type = _normalize_agent_type(body.get("agent_type"))
    debug = bool(body.get("debug"))
    thread_id = body.get("thread_id")

    user = premium_user(request)
    persist = False
    chat_user_id: str | None = None
    thread: ChatThread | None = None

    if user and thread_id is not None and str(thread_id).isdigit():
        with get_session() as db:
            t = db.get(ChatThread, int(thread_id))
            if not t or t.user_id != user.id:
                return JSONResponse({"error": "Conversation not found"}, status_code=404)
            thread = t
            adk_session_id = t.adk_session_id
            agent_type = t.agent_type  # trust the stored thread's agent type
        persist = True
        chat_user_id = f"user-{user.id}"
    else:
        session_key = (
            ADK_TRADING_CHAT_SESSION_KEY if agent_type == "trading" else ADK_CHAT_SESSION_KEY
        )
        adk_session_id = ensure_adk_chat_session_id(request, session_key)

    approval_result = None
    if agent_type == "trading":
        approval_result = _try_handle_approval(sid, message)

    effective_message = message
    if approval_result:
        effective_message = f"[System: {approval_result}] {message}"

    try:
        result = await registry.chat(
            angel_sid=sid,
            adk_session_id=adk_session_id,
            message=effective_message,
            agent_type=agent_type,
            debug=debug,
            user_id=chat_user_id,
            persist=persist,
        )
        if approval_result:
            result["approval_result"] = approval_result
        if thread is not None:
            _touch_thread(thread.id, message)
            result["thread_id"] = thread.id
        return JSONResponse(result)
    except ValueError as e:
        err = str(e)
        if "OPENROUTER_API_KEY" in err:
            return JSONResponse({"error": err}, status_code=503)
        return JSONResponse({"error": err}, status_code=400)
    except Exception as e:
        logger.exception("ADK agent chat error")
        return JSONResponse({"error": f"Agent error: {e}"}, status_code=500)



def _try_handle_approval(sid: str, message: str) -> str | None:
    """If message is APPROVE/REJECT <id>, handle it server-side and return a status string."""
    m = _APPROVE_RE.match(message.strip())
    if m:
        pid = m.group(1)
        try:
            client = sessions.get_client(sid)
            proposal_store.approve(sid, pid)
            if client:
                result = execute_proposal(sid, pid, client)
                if result.get("ok"):
                    return f"Proposal {pid} APPROVED and executed. Order ID: {result.get('order_id')}"
                return f"Proposal {pid} APPROVED but execution failed: {result.get('error')}"
            return f"Proposal {pid} approved but no broker session found."
        except (ValueError, PermissionError) as e:
            return f"Approval failed: {e}"

    m = _REJECT_RE.match(message.strip())
    if m:
        pid = m.group(1)
        try:
            proposal_store.reject(sid, pid)
            return f"Proposal {pid} REJECTED."
        except (ValueError, PermissionError) as e:
            return f"Rejection failed: {e}"

    return None



@router.post("/api/agent/new-chat")
async def api_agent_new_chat(request: Request):
    """Start a fresh ADK thread (new id in the signed session cookie)."""
    sid = session_id(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    agent_type = (body.get("agent_type") or "finance").strip().lower()

    if agent_type == "trading":
        request.session[ADK_TRADING_CHAT_SESSION_KEY] = uuid.uuid4().hex
    else:
        request.session[ADK_CHAT_SESSION_KEY] = uuid.uuid4().hex
    return JSONResponse({"ok": True})
