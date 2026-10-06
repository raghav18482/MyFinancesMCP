"""Daily briefing schedule management and manual sends."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import select

from db import get_session
from db.models import Log, Schedule, User

from services.realtime_feed import feed_relay

from session_manager import sessions

from web.dependencies import (
    current_client,
    registered_user_for_session,
)

logger = logging.getLogger(__name__)

router = APIRouter()



# ── Briefing Scheduler API ─────────────────────────────────────────────────

_IST = timezone(timedelta(hours=5, minutes=30))


_BRIEFING_KIND = "daily_briefing"



def _next_run_utc_from_ist(time_ist: str) -> datetime:
    """Convert 'HH:MM' IST → next UTC naive datetime for that wall-clock time."""
    hour, minute = map(int, time_ist.split(":"))
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(_IST)
    candidate = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_ist:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)



def _get_briefing_schedule(user_id: int) -> Schedule | None:
    with get_session() as db:
        return db.exec(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.kind == _BRIEFING_KIND,
            )
        ).first()



def _schedule_to_dict(s: Schedule) -> dict:
    return {
        "id": s.id,
        "enabled": s.enabled,
        "interval_minutes": s.interval_minutes,
        "next_run": s.next_run.isoformat() if s.next_run else None,
        "last_run": s.last_run.isoformat() if s.last_run else None,
        "status": s.status,
    }



@router.get("/api/briefing/schedule")
async def api_briefing_schedule_get(request: Request):
    client = current_client(request)
    if not client:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = registered_user_for_session(client)
    if not user:
        return JSONResponse({"exists": False})
    schedule = _get_briefing_schedule(user.id)
    if not schedule:
        return JSONResponse({"exists": False})
    return JSONResponse({"exists": True, "schedule": _schedule_to_dict(schedule)})



@router.post("/api/briefing/schedule")
async def api_briefing_schedule_save(request: Request):
    client = current_client(request)
    if not client:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = registered_user_for_session(client)
    if not user:
        return JSONResponse({"error": "Not registered for premium"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    time_ist = (body.get("time_ist") or "08:30").strip()
    try:
        next_run = _next_run_utc_from_ist(time_ist)
    except Exception:
        return JSONResponse({"error": "Invalid time format, expected HH:MM"}, status_code=400)
    enabled = bool(body.get("enabled", True))
    with get_session() as db:
        schedule = db.exec(
            select(Schedule).where(
                Schedule.user_id == user.id,
                Schedule.kind == _BRIEFING_KIND,
            )
        ).first()
        if schedule:
            schedule.next_run = next_run
            schedule.enabled = enabled
            schedule.status = "pending"
            db.add(schedule)
        else:
            schedule = Schedule(
                user_id=user.id,
                kind=_BRIEFING_KIND,
                interval_minutes=1440,
                next_run=next_run,
                enabled=enabled,
                status="pending",
            )
            db.add(schedule)
        db.commit()
        db.refresh(schedule)
        return JSONResponse({"ok": True, "schedule": _schedule_to_dict(schedule)})



@router.delete("/api/briefing/schedule")
async def api_briefing_schedule_delete(request: Request):
    client = current_client(request)
    if not client:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = registered_user_for_session(client)
    if not user:
        return JSONResponse({"error": "Not registered"}, status_code=403)
    with get_session() as db:
        schedule = db.exec(
            select(Schedule).where(
                Schedule.user_id == user.id,
                Schedule.kind == _BRIEFING_KIND,
            )
        ).first()
        if not schedule:
            return JSONResponse({"ok": True})
        schedule.enabled = False
        db.add(schedule)
        db.commit()
    return JSONResponse({"ok": True})



async def _run_briefing_now_bg(user_id: int, phone: str | None) -> None:
    """Background task: full briefing pipeline without touching next_run."""
    import time as _time
    from uuid import uuid4 as _uuid4
    from db import decrypt_value as _dec
    from services.schedular.daily_briefing import generate_daily_briefing as _brief
    from services.schedular.whatsapp import send as _wa

    started = _time.perf_counter()
    sid = f"manual-{user_id}-{_uuid4().hex[:8]}"
    try:
        with get_session() as db:
            user = db.get(User, user_id)
            if not user or not user.is_active:
                return
            api_key = user.angel_api_key
            client_id = user.angel_client_id
            pw = _dec(user.angel_password_encrypted)
            totp = _dec(user.angel_totp_secret_encrypted)

        sessions.create_session(sid, api_key, client_id, pw, totp)
        message = await _brief(sid)
        await _wa(phone, message)
        duration_ms = int((_time.perf_counter() - started) * 1000)
        with get_session() as db:
            db.add(Log(
                user_id=user_id,
                status="success",
                message=f"manual send · {len(message)} chars to {phone or '<unset>'}",
                duration_ms=duration_ms,
            ))
            db.commit()
    except Exception:
        duration_ms = int((_time.perf_counter() - started) * 1000)
        logger.exception("manual briefing send failed for user %s", user_id)
        try:
            with get_session() as db:
                db.add(Log(user_id=user_id, status="failed",
                           message="manual send error", duration_ms=duration_ms))
                db.commit()
        except Exception:
            pass
    finally:
        try:
            feed_relay.stop_feed(sid)
            sessions.remove_session(sid)
        except Exception:
            pass



@router.post("/api/briefing/send-now")
async def api_briefing_send_now(request: Request):
    client = current_client(request)
    if not client:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = registered_user_for_session(client)
    if not user:
        return JSONResponse({"error": "Not registered for premium"}, status_code=403)
    asyncio.create_task(_run_briefing_now_bg(user.id, user.whatsapp_number))
    return JSONResponse({"ok": True, "message": "Briefing started — check WhatsApp in ~30s"})



@router.get("/api/briefing/logs")
async def api_briefing_logs(request: Request):
    client = current_client(request)
    if not client:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = registered_user_for_session(client)
    if not user:
        return JSONResponse({"logs": []})
    with get_session() as db:
        logs = db.exec(
            select(Log)
            .where(Log.user_id == user.id)
            .order_by(Log.created_at.desc())
            .limit(8)
        ).all()
    return JSONResponse({
        "logs": [
            {
                "id": l.id,
                "status": l.status,
                "message": l.message,
                "duration_ms": l.duration_ms,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ]
    })
