"""Shared SQLModel engine + session factory + bootstrap.

One process-wide ``engine`` reads ``DATABASE_URL`` from env (Postgres by
default). ``init_db()`` is idempotent and safe to run on every startup until
Alembic is introduced.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql+psycopg://my_finance:admin@localhost/my_finance"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)

# Neon (and other SSL-required hosts) embed sslmode=require in the URL.
# psycopg v3 needs it forwarded explicitly via connect_args when using the
# pooler endpoint. Passing an empty dict is harmless for local connections.
_url_lower = DATABASE_URL.lower()
_connect_args: dict = {"sslmode": "require"} if "sslmode=require" in _url_lower else {}

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True, connect_args=_connect_args)


# Lightweight per-column migrations applied on startup until Alembic is
# introduced. ``create_all`` only creates missing *tables*; it never adds a
# column to a table that already exists, so every new column on an existing
# table must be listed here. Keep new columns nullable with no default, which
# makes the ALTER a metadata-only change.
#
# (table, column, statement). The statement runs only when the column is
# missing, so a normal startup takes no lock on these tables at all.
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("schedules", "status",
     "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'pending'"),
    # No default: NULL means "this user has not set an app password yet", which
    # the login path treats as "cannot log in, re-enroll to set one".
    ("users", "app_password_hash",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS app_password_hash VARCHAR"),
    # Logout bumps this; cookies issued with an older value stop working.
    ("users", "session_version",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0"),
    # Scheduler leases (services/schedular/repository.py): who is running a
    # schedule's current slot, since when, and until when that claim holds.
    ("schedules", "claimed_at",
     "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ"),
    ("schedules", "lease_expires_at",
     "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ"),
    ("schedules", "claim_token",
     "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS claim_token VARCHAR(32)"),
    # Manual "send now" cooldown, checked and set in one atomic UPDATE.
    ("users", "last_manual_briefing_at",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_manual_briefing_at TIMESTAMPTZ"),
)

#: How long startup DDL may wait for a lock before giving up. An ALTER queued
#: behind a long transaction would block every later query on that table.
LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "30s"


def init_db(*, bind=None, lock_timeout: str = LOCK_TIMEOUT) -> None:
    """Create missing tables, then add missing columns, in one transaction.

    All or nothing: if any statement fails, including a lock wait longer than
    ``lock_timeout``, nothing is applied and the error propagates. The caller
    decides whether that stops startup (it does in production; see ``main``).

    Idempotent; replace with Alembic when schema evolves beyond simple adds.
    """
    from . import models  # noqa: F401  -- ensure models register with SQLModel.metadata

    target = bind if bind is not None else engine
    with target.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
            conn.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
        SQLModel.metadata.create_all(conn)
        if conn.dialect.name == "postgresql":
            applied = _add_missing_columns(conn)
            if applied:
                logger.info("init_db: added columns %s", ", ".join(applied))

    logger.info("init_db: tables ensured on %s", _safe_url(str(target.url)))


def _add_missing_columns(conn) -> list[str]:
    existing = {
        (row.table_name, row.column_name)
        for row in conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        ))
    }
    applied = []
    for table, column, stmt in _COLUMN_MIGRATIONS:
        if (table, column) not in existing:
            conn.execute(text(stmt))
            applied.append(f"{table}.{column}")
    return applied


def get_session() -> Session:
    """Return a new SQLModel session bound to the shared engine."""
    return Session(engine)


def _safe_url(url: str) -> str:
    """Strip credentials from the DATABASE_URL for logs."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    _, host = rest.split("@", 1)
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"
