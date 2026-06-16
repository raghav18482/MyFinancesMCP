"""SQLModel tables: ``users`` (with encrypted creds), ``schedules``, ``logs``.

Schema is intentionally small. New columns can be added column-by-column as
the scheduler grows; full Alembic migrations should be introduced the day the
schema first changes after this commit.
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    whatsapp_number: str | None = Field(default=None, index=True, unique=True)

    angel_api_key: str
    angel_client_id: str = Field(index=True, unique=True)
    angel_password_encrypted: str
    angel_totp_secret_encrypted: str
    angel_access_token: str | None = None

    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Schedule(SQLModel, table=True):
    __tablename__ = "schedules"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    kind: str = Field(default="daily_briefing")
    interval_minutes: int
    next_run: datetime = Field(index=True)
    last_run: datetime | None = None
    enabled: bool = Field(default=True, index=True)
    status: str = Field(default="pending", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Log(SQLModel, table=True):
    __tablename__ = "logs"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    schedule_id: int | None = Field(default=None, foreign_key="schedules.id")
    status: str
    message: str | None = None
    duration_ms: int | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class RiskProfile(SQLModel, table=True):
    __tablename__ = "risk_profiles"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True, unique=True)
    age: int
    goal: str
    horizon_years: int
    risk_tolerance: str
    tax_bracket: str
    max_single_order_value: float
    max_position_pct: float
    allowed_products: str  # comma-separated e.g. "DELIVERY" or "DELIVERY,INTRADAY"
    max_daily_trades: int
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ChatThread(SQLModel, table=True):
    """One persisted agent conversation (sidebar entry).

    Each thread maps to exactly one ADK session via ``adk_session_id``; the
    conversation turns themselves live in ADK's DatabaseSessionService tables.
    """

    __tablename__ = "chat_threads"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    agent_type: str = Field(default="finance", index=True)  # "finance" | "trading"
    adk_session_id: str = Field(index=True, unique=True)
    title: str = Field(default="New conversation")
    archived: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)
