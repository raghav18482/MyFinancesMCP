"""Admin REST API — manage users (with encrypted Angel credentials) and schedules.

All endpoints require the ``X-Admin-Key`` header to match the ``ADMIN_API_KEY``
env var (at least 32 characters), from an allowed source: by default only
from inside the instance, over loopback. In production the router is mounted
only with ``ADMIN_API_ENABLED=1``. Never returns secrets, including the Angel API
key, in any response. See docs/runbooks/admin-api.md.

Mount in web/app.py with::

    from adminApi import admin_router
    web.include_router(admin_router)

Then browse to  http://localhost:8000/docs  to test via Swagger UI.
"""
from __future__ import annotations

import asyncio
import logging
import hmac
import os
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlmodel import select

from .guard import guard
from .service import UserAlreadyExists, UserNotFound
from . import service
from db import get_session
from db.models import Schedule, User
from web import security_config

logger = logging.getLogger(__name__)

admin_router = APIRouter(prefix="/api/admin", tags=["Admin"])


# ── Auth ─────────────────────────────────────────────────────────────────────

_MISCONFIG_ALERT_EVERY_SEC = 600
_last_misconfig_alert: Optional[float] = None


async def check_admin_key(
    request: Request,
    x_admin_key: str = Header(..., description="Value of ADMIN_API_KEY env var"),
) -> None:
    """Allow a request only from an allowed source, with the right key.

    Order matters: the source check comes first (anything else is a 404, as if
    the API did not exist), then the key. A correct key always passes, whatever
    the failure counters say, so a flood of wrong keys cannot lock the admin out.
    """
    global _last_misconfig_alert

    source = security_config.admin_request_source(request)
    if source is None:
        raise HTTPException(status_code=404, detail="Not Found")

    if not security_config.admin_key_usable():
        now = time.monotonic()
        if _last_misconfig_alert is None or now - _last_misconfig_alert >= _MISCONFIG_ALERT_EVERY_SEC:
            _last_misconfig_alert = now
            logger.error("ADMIN_KEY_MISCONFIGURED status=%s", security_config.admin_key_status())
        raise HTTPException(status_code=503, detail="Admin API is not configured.")

    expected = os.environ.get("ADMIN_API_KEY", "").strip()
    # Constant time, so response timing says nothing about how much of a guess matched.
    if hmac.compare_digest(x_admin_key.encode("utf-8"), expected.encode("utf-8")):
        return

    retry = guard.retry_after(source)
    if retry is not None:
        raise HTTPException(status_code=429, detail="Too many attempts.",
                            headers={"Retry-After": str(retry)})
    delay = guard.record_failure(source)
    if delay:
        await asyncio.sleep(delay)
    raise HTTPException(status_code=401, detail="Invalid admin key.")


# ── Request schemas ──────────────────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    whatsapp_number: str
    angel_api_key: str
    angel_client_id: str
    angel_password: str
    angel_totp_secret: str

    @field_validator("whatsapp_number")
    @classmethod
    def validate_whatsapp(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("+"):
            raise ValueError("whatsapp_number must start with + (e.g. +919999999999)")
        return v


class UserUpdateRequest(BaseModel):
    whatsapp_number: Optional[str] = None
    angel_api_key: Optional[str] = None
    angel_password: Optional[str] = None
    angel_totp_secret: Optional[str] = None
    is_active: Optional[bool] = None


class ScheduleCreateRequest(BaseModel):
    kind: str = "daily_briefing"
    interval_minutes: int
    next_run: datetime
    enabled: bool = True

    @field_validator("interval_minutes")
    @classmethod
    def validate_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("interval_minutes must be >= 1")
        return v


class ScheduleUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = None
    next_run: Optional[datetime] = None
    kind: Optional[str] = None


# ── Response schemas ─────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    id: int
    whatsapp_number: Optional[str]
    angel_client_id: str
    has_broker_credentials: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScheduleResponse(BaseModel):
    id: int
    user_id: int
    kind: str
    interval_minutes: int
    next_run: datetime
    last_run: Optional[datetime]
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── User endpoints ────────────────────────────────────────────────────────────

@admin_router.post(
    "/users",
    response_model=UserResponse,
    status_code=201,
    summary="Create a user with encrypted Angel credentials",
)
def create_user(
    body: UserCreateRequest,
    _: None = Depends(check_admin_key),
) -> UserResponse:
    try:
        view = service.create_user(
            whatsapp_number=body.whatsapp_number,
            angel_api_key=body.angel_api_key,
            angel_client_id=body.angel_client_id,
            angel_password=body.angel_password,
            angel_totp_secret=body.angel_totp_secret,
        )
    except UserAlreadyExists as e:
        raise HTTPException(status_code=409, detail=str(e))
    return UserResponse.model_validate(view)


@admin_router.get(
    "/users",
    response_model=list[UserResponse],
    summary="List all users (no secrets returned)",
)
def list_users(_: None = Depends(check_admin_key)) -> list[UserResponse]:
    return [UserResponse.model_validate(v) for v in service.list_users()]


@admin_router.get(
    "/users/{user_id}",
    response_model=UserResponse,
    summary="Get a single user by ID",
)
def get_user(user_id: int, _: None = Depends(check_admin_key)) -> UserResponse:
    try:
        return UserResponse.model_validate(service.get_user(user_id))
    except UserNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@admin_router.put(
    "/users/{user_id}",
    response_model=UserResponse,
    summary="Update user fields (re-encrypts password/TOTP if provided)",
)
def update_user(
    user_id: int,
    body: UserUpdateRequest,
    _: None = Depends(check_admin_key),
) -> UserResponse:
    try:
        view = service.update_user(
            user_id,
            whatsapp_number=body.whatsapp_number,
            angel_api_key=body.angel_api_key,
            angel_password=body.angel_password,
            angel_totp_secret=body.angel_totp_secret,
            is_active=body.is_active,
        )
    except UserNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return UserResponse.model_validate(view)


@admin_router.delete(
    "/users/{user_id}",
    summary="Soft-delete a user (sets is_active=False, keeps DB row for logs FK integrity)",
)
def deactivate_user(user_id: int, _: None = Depends(check_admin_key)) -> dict:
    try:
        service.deactivate_user(user_id)
    except UserNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "user_id": user_id, "is_active": False}


# ── Schedule endpoints ────────────────────────────────────────────────────────

@admin_router.post(
    "/users/{user_id}/schedules",
    response_model=ScheduleResponse,
    status_code=201,
    summary="Create a schedule for a user",
)
def create_schedule(
    user_id: int,
    body: ScheduleCreateRequest,
    _: None = Depends(check_admin_key),
) -> ScheduleResponse:
    with get_session() as session:
        user = session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found.")
        schedule = Schedule(
            user_id=user_id,
            kind=body.kind,
            interval_minutes=body.interval_minutes,
            next_run=body.next_run,
            enabled=body.enabled,
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        logger.info(
            "admin: created schedule id=%s user_id=%s kind=%s interval=%dmin",
            schedule.id, user_id, schedule.kind, schedule.interval_minutes,
        )
        return ScheduleResponse.model_validate(schedule)


@admin_router.get(
    "/users/{user_id}/schedules",
    response_model=list[ScheduleResponse],
    summary="List all schedules for a user",
)
def list_schedules(user_id: int, _: None = Depends(check_admin_key)) -> list[ScheduleResponse]:
    with get_session() as session:
        schedules = session.exec(
            select(Schedule).where(Schedule.user_id == user_id)
        ).all()
        return [ScheduleResponse.model_validate(s) for s in schedules]


@admin_router.put(
    "/schedules/{schedule_id}",
    response_model=ScheduleResponse,
    summary="Update a schedule (enable/disable, change interval or next_run)",
)
def update_schedule(
    schedule_id: int,
    body: ScheduleUpdateRequest,
    _: None = Depends(check_admin_key),
) -> ScheduleResponse:
    with get_session() as session:
        schedule = session.get(Schedule, schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found.")
        if body.enabled is not None:
            schedule.enabled = body.enabled
        if body.interval_minutes is not None:
            if body.interval_minutes < 1:
                raise HTTPException(status_code=422, detail="interval_minutes must be >= 1")
            schedule.interval_minutes = body.interval_minutes
        if body.next_run is not None:
            schedule.next_run = body.next_run
        if body.kind is not None:
            schedule.kind = body.kind
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        logger.info("admin: updated schedule id=%s", schedule_id)
        return ScheduleResponse.model_validate(schedule)
