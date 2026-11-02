"""SQLModel tables: ``users`` (with encrypted creds), ``schedules``, ``logs``.

Schema is intentionally small. New columns can be added column-by-column as
the scheduler grows; full Alembic migrations should be introduced the day the
schema first changes after this commit.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
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
    __table_args__ = (
        CheckConstraint("firebase_uid IS NULL OR length(firebase_uid) BETWEEN 1 AND 128",
                        name="ck_users_firebase_uid"),
        CheckConstraint("auth_phone_e164 IS NULL OR auth_phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'",
                        name="ck_users_auth_phone").ddl_if(dialect="postgresql"),
        Index("uq_users_firebase_uid", "firebase_uid", unique=True,
              postgresql_where=text("firebase_uid IS NOT NULL"), sqlite_where=text("firebase_uid IS NOT NULL")),
        Index("uq_users_auth_phone", "auth_phone_e164", unique=True,
              postgresql_where=text("auth_phone_e164 IS NOT NULL"),
              sqlite_where=text("auth_phone_e164 IS NOT NULL")),
    )

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

    # Bumped on logout. The signed session cookie records the value it was
    # issued with (``uv``) and is only honoured while the two match, which is
    # what makes logout revoke every saved copy of a stateless cookie.
    session_version: int = Field(default=0)

    # Last manual "send briefing now"; the cooldown is checked and set atomically.
    last_manual_briefing_at: datetime | None = None

    # For the sign-in that will not depend on Angel One (revision 0003; not
    # used yet). auth_phone_e164 is the number signed in with, kept apart from
    # whatsapp_number, which is where briefings go.
    firebase_uid: str | None = Field(default=None, sa_column=Column(Text))
    auth_phone_e164: str | None = Field(default=None, sa_column=Column(Text))
    email: str | None = Field(default=None, sa_column=Column(Text))
    last_login_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class McpToken(SQLModel, table=True):
    """A personal access token for the read-only MCP endpoint.

    Custom bearer tokens issued from the web app, not OAuth. Only a SHA-256
    hash of the token is stored; the raw value is shown to the user once.
    """

    __tablename__ = "mcp_tokens"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(default="MCP client")
    token_prefix: str  # first characters, for display only
    token_hash: str = Field(index=True, unique=True)
    scopes: str = Field(default="portfolio:read")  # space-separated
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


# ── OAuth 2.1 for the MCP endpoint (see services/oauth_provider.py) ───────────
# New tables only, so ``create_all`` adds them and no column migration is
# needed. Every secret (client secret, code, access/refresh token) is stored as
# a SHA-256 hash; the raw values exist only in the HTTP response that issues them.


class OAuthClient(SQLModel, table=True):
    """A client registered through Dynamic Client Registration (RFC 7591)."""

    __tablename__ = "oauth_clients"

    id: int | None = Field(default=None, primary_key=True)
    client_id: str = Field(index=True, unique=True)
    client_secret_hash: str | None = None  # None for public clients (auth method "none")
    # The registered metadata as JSON, minus any secret. Redirect URIs are in here.
    metadata_json: str
    client_name: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class OAuthGrant(SQLModel, table=True):
    """One user's consent for one client. Revoking it kills every token it issued."""

    __tablename__ = "oauth_grants"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    client_id: str = Field(index=True)
    scopes: str  # space-separated
    resource: str  # RFC 8707: the MCP endpoint these tokens are for
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class OAuthAuthCode(SQLModel, table=True):
    """A short-lived, single-use authorization code (PKCE S256)."""

    __tablename__ = "oauth_codes"

    id: int | None = Field(default=None, primary_key=True)
    code_hash: str = Field(index=True, unique=True)
    client_id: str = Field(index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    scopes: str
    resource: str
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    used_at: datetime | None = None
    # Set when the code is exchanged, so a replayed code can revoke what it issued.
    grant_id: int | None = Field(default=None, foreign_key="oauth_grants.id")


class OAuthToken(SQLModel, table=True):
    """An access or refresh token belonging to a grant."""

    __tablename__ = "oauth_tokens"

    id: int | None = Field(default=None, primary_key=True)
    grant_id: int = Field(foreign_key="oauth_grants.id", index=True)
    kind: str = Field(index=True)  # "access" | "refresh"
    token_hash: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    # Refresh tokens rotate: set when exchanged. Presenting it again is reuse.
    used_at: datetime | None = None
    revoked_at: datetime | None = None


class Schedule(SQLModel, table=True):
    __tablename__ = "schedules"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    kind: str = Field(default="daily_briefing")
    interval_minutes: int
    next_run: datetime = Field(index=True)
    last_run: datetime | None = None
    enabled: bool = Field(default=True, index=True)
    # pending | running | done | failed (known not sent) | unknown (outcome lost)
    status: str = Field(default="pending", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    # The R5 scheduler's lease on a run. Unused from R10 (the delivery ledger
    # holds the lease); kept for rows R5 left running, see repository.reclaim_expired.
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    claim_token: str | None = Field(default=None, max_length=32)


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

    Historical: rows were written when the app could still execute approved
    proposals (on success, on compliance block, and on failure). Execution has
    since been removed, so nothing writes new rows today; the table is kept,
    unaltered, as audit history. Rows are only ever shown to their owner
    (``user_id``); rows with no owner are never shown to any user.

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


class AppInstance(SQLModel, table=True):
    """One running server process, refreshed every minute.

    The app keeps broker sessions, throttles and caches in process memory, so it
    must run as a single instance. ``services.instance_registry`` reports when
    more than one is alive for longer than a deploy overlap.
    """

    __tablename__ = "app_instances"

    instance_id: str = Field(primary_key=True)  # "<hostname>:<pid>:<random>"
    started_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow, index=True)


class FeedbackSubmission(SQLModel, table=True):
    """Feedback from the public form, stored while WhatsApp replies are off.

    Deliberately minimal: no name, phone number or IP address.
    """

    __tablename__ = "feedback_submissions"
    __table_args__ = (
        CheckConstraint("length(feedback) BETWEEN 1 AND 1000", name="ck_feedback_length"),
        CheckConstraint("channel IN ('react', 'jinja', 'api')", name="ck_feedback_channel"),
    )

    id: int | None = Field(default=None, primary_key=True)
    feedback: str
    channel: str
    created_at: datetime = Field(default_factory=utcnow, index=True)


class BriefingDelivery(SQLModel, table=True):
    """One briefing send: a scheduled slot or a manual request, and its outcome.

    The schema is Alembic revision ``0002_delivery_ledger``; this model mirrors
    it for queries and for local databases built with ``create_all``. The
    states and their transitions live in ``services/schedular/ledger.py``.
    """

    __tablename__ = "briefing_deliveries"
    __table_args__ = (
        CheckConstraint("kind IN ('scheduled', 'manual')", name="ck_bd_kind"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'accepted', 'not_sent_retryable', 'failed_final', "
            "'unknown', 'skipped_stale')",
            name="ck_bd_status",
        ),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_bd_attempts"),
        CheckConstraint(
            "outcome_reason IS NULL OR outcome_reason IN ('not_ready', 'connect', 'deadline', 'http_4xx', "
            "'auth', 'bad_phone', 'generation', 'http_5xx', 'http_503', 'http_other', 'transport', "
            "'inconsistent', 'stale', 'disabled', 'lease_expired')",
            name="ck_bd_reason",
        ),
        CheckConstraint("http_status IS NULL OR http_status BETWEEN 100 AND 599", name="ck_bd_http_status"),
        CheckConstraint("(kind = 'scheduled') = (schedule_id IS NOT NULL)", name="ck_bd_schedule"),
        CheckConstraint(
            "(status IN ('pending', 'sending')) = (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_bd_lease",
        ),
        CheckConstraint("(status = 'accepted') = (accepted_at IS NOT NULL)", name="ck_bd_accepted"),
        CheckConstraint(
            "status <> 'not_sent_retryable' OR (kind = 'scheduled' AND next_attempt_at IS NOT NULL)",
            name="ck_bd_retry",
        ),
        CheckConstraint("status <> 'sending' OR attempts >= 1", name="ck_bd_sending"),
        CheckConstraint(
            "idempotency_key ~ '^(sched:[0-9]+:[0-9]{8}T[0-9]{6}Z|manual:[0-9]+:[0-9a-f]{32})$'",
            name="ck_bd_key",
        ).ddl_if(dialect="postgresql"),
        Index("uq_bd_sched_slot", "schedule_id", "slot_at", unique=True,
              postgresql_where=text("kind = 'scheduled'"), sqlite_where=text("kind = 'scheduled'")),
        Index("uq_bd_manual_inflight", "user_id", unique=True,
              postgresql_where=text("kind = 'manual' AND status IN ('pending', 'sending')"),
              sqlite_where=text("kind = 'manual' AND status IN ('pending', 'sending')")),
        Index("ix_bd_retry", "status", "next_attempt_at"),
        Index("ix_bd_lease", "status", "lease_expires_at"),
        Index("ix_bd_user", "user_id"),
    )

    id: int | None = Field(default=None, sa_column=Column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True))
    kind: str = Field(sa_column=Column(Text, nullable=False))
    schedule_id: int | None = Field(default=None, sa_column=Column(
        Integer, ForeignKey("schedules.id", ondelete="RESTRICT"), nullable=True))
    user_id: int = Field(sa_column=Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False))
    slot_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    idempotency_key: str = Field(sa_column=Column(Text, nullable=False, unique=True))
    status: str = Field(sa_column=Column(Text, nullable=False))
    attempts: int = Field(default=0, sa_column=Column(SmallInteger, nullable=False, server_default="0"))
    next_attempt_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    lease_expires_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    claim_token: str | None = Field(default=None, sa_column=Column(String(32)))
    outcome_reason: str | None = Field(default=None, sa_column=Column(Text))
    http_status: int | None = Field(default=None, sa_column=Column(SmallInteger))
    accepted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    row_version: int = Field(default=1, sa_column=Column(Integer, nullable=False, server_default="1"))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))


class BrokerConnection(SQLModel, table=True):
    """A user's link to their broker: account id and encrypted secrets.

    Revision ``0003_broker_connections``; mirrored here. Not read or written
    by the app yet (the backfill and dual-write come next). Secrets are Fernet
    ciphertext under data key ``key_version`` (``db.crypto``).
    """

    __tablename__ = "broker_connections"
    __table_args__ = (
        CheckConstraint("broker IN ('angelone')", name="ck_bc_broker"),
        CheckConstraint(
            "broker_account_id = upper(btrim(broker_account_id)) AND broker_account_id ~ '^[A-Z0-9]{4,16}$'",
            name="ck_bc_account_id",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "api_key_encrypted ~ '^gAAAAA[A-Za-z0-9_-]+={0,2}$' AND length(api_key_encrypted) BETWEEN 100 AND 4096",
            name="ck_bc_api_key",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "password_encrypted ~ '^gAAAAA[A-Za-z0-9_-]+={0,2}$' AND length(password_encrypted) BETWEEN 100 AND 4096",
            name="ck_bc_password",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "totp_secret_encrypted ~ '^gAAAAA[A-Za-z0-9_-]+={0,2}$' "
            "AND length(totp_secret_encrypted) BETWEEN 100 AND 4096",
            name="ck_bc_totp",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("key_version >= 1", name="ck_bc_key_version"),
        CheckConstraint("status IN ('active', 'needs_reauth', 'disconnected')", name="ck_bc_status"),
        CheckConstraint("created_by IN ('app', 'backfill')", name="ck_bc_created_by"),
        CheckConstraint("(created_by = 'backfill') = (backfill_run_id IS NOT NULL)", name="ck_bc_backfill_run"),
        UniqueConstraint("user_id", "broker", name="uq_bc_user_broker"),
        UniqueConstraint("broker", "broker_account_id", name="uq_bc_account"),
    )

    id: int | None = Field(default=None, sa_column=Column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True))
    user_id: int = Field(sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False))
    broker: str = Field(sa_column=Column(Text, nullable=False))
    broker_account_id: str = Field(sa_column=Column(Text, nullable=False))
    api_key_encrypted: str = Field(sa_column=Column(Text, nullable=False))
    password_encrypted: str = Field(sa_column=Column(Text, nullable=False))
    totp_secret_encrypted: str = Field(sa_column=Column(Text, nullable=False))
    key_version: int = Field(sa_column=Column(Integer, nullable=False))
    status: str = Field(default="active", sa_column=Column(Text, nullable=False, server_default="active"))
    created_by: str = Field(default="app", sa_column=Column(Text, nullable=False, server_default="app"))
    backfill_run_id: uuid.UUID | None = Field(default=None, sa_column=Column(Uuid))
    last_verified_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    last_error_code: str | None = Field(default=None, sa_column=Column(Text))
    row_version: int = Field(default=1, sa_column=Column(Integer, nullable=False, server_default="1"))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
