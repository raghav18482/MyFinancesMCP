"""SQLModel tables: ``users`` (with encrypted creds), ``schedules``, ``logs``.

Schema is intentionally small. New columns can be added column-by-column as
the scheduler grows; full Alembic migrations should be introduced the day the
schema first changes after this commit.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    SQLModel maps ``datetime`` columns through ``UTCDateTime``, whose bind
    processor rejects naive values outright — on every backend, not just
    SQLite. ``datetime.utcnow`` returns a naive value, so using it as a default
    factory makes every insert raise "Datetime values must have timezone
    information". Keep this the single source of the timestamp default.
    """
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    whatsapp_number: str | None = Field(default=None, index=True, unique=True)

    angel_api_key: str
    angel_client_id: str = Field(index=True, unique=True)
    angel_password_encrypted: str
    angel_totp_secret_encrypted: str
    angel_access_token: str | None = None

    # scrypt hash of the password used to log into *this* app, from
    # ``db.passwords``. One-way on purpose: unlike the Angel fields above, the
    # server never needs to read it back. Nullable because rows created by
    # premium registration predate app logins — those users set one by
    # re-enrolling at ``/enroll``, which re-proves their Angel credentials.
    app_password_hash: str | None = None

    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


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
    created_at: datetime = Field(default_factory=utcnow)


class Log(SQLModel, table=True):
    __tablename__ = "logs"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    schedule_id: int | None = Field(default=None, foreign_key="schedules.id")
    status: str
    message: str | None = None
    duration_ms: int | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)


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
    updated_at: datetime = Field(default_factory=utcnow)


class DecisionRecord(SQLModel, table=True):
    """Append-only record of every trade decision, including blocked ones.

    Written from ``services.trade_proposals.execute_proposal`` on success, on
    compliance block, and on execution failure. A blocked trade is precisely
    the thing an audit asks about, so it is recorded rather than discarded.

    There is deliberately no update path. Correcting a record means writing a
    new one; mutating history is the thing this table exists to prevent.
    """

    __tablename__ = "decision_records"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    session_ref: str | None = Field(default=None, index=True)

    proposal_id: str = Field(index=True)
    symbol: str = Field(index=True)
    side: str
    quantity: float
    price: float | None = None

    # "executed" | "blocked" | "failed" | "rejected"
    outcome: str = Field(index=True)
    approved_by: str | None = None
    order_id: str | None = None

    # Hash of the inputs the decision was made on, so the payload can be shown
    # to be unaltered without storing a second copy of it.
    payload_hash: str | None = None
    payload_json: str | None = None

    # Which model and feature contract informed it, so a decision can be
    # reproduced against the exact artefacts that produced it.
    model_version: str | None = None
    feature_set_version: int | None = None
    model_scores_json: str | None = None

    # Which ruleset was in force, and what it said.
    ruleset_id: str | None = None
    compliance_json: str | None = None

    rationale: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)


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
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
