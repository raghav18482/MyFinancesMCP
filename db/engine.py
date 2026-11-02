"""Shared SQLModel engine + session factory + bootstrap.

One process-wide ``engine`` reads ``DATABASE_URL`` from env (Postgres by
default).

From R7 Alembic owns the schema of any database it has stamped, production
first. ``init_db()`` then only checks the revision (refusing to start in
production on one this release does not list, see :mod:`db.schema_version`).
A local database Alembic has never touched still gets ``create_all`` plus the
column additions below, as before.
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


# Per-column additions for local databases Alembic does not manage. Frozen
# from R7: new schema changes are Alembic revisions (migrations/versions/).
# ``create_all`` only creates missing *tables*; it never adds a column to a
# table that already exists, which is why these were listed here.
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
    # The R5 scheduler's leases on schedules (superseded by the delivery
    # ledger in R10; kept so older rows stay readable).
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


def init_db(*, bind=None, lock_timeout: str = LOCK_TIMEOUT, production: bool = False) -> None:
    """Check the schema revision, or (unmanaged local databases) create the schema.

    ``production``: run no DDL at all. The database must be at a revision in
    ``schema_version.COMPATIBLE_REVISIONS``, otherwise
    :class:`db.schema_version.IncompatibleSchema` is raised and startup stops.

    Otherwise, a database Alembic manages is left alone (a warning if its
    revision is not one this release lists). A database it does not manage
    gets missing tables, then missing columns, in one transaction: all or
    nothing, including a lock wait longer than ``lock_timeout``.
    """
    from . import models  # noqa: F401  -- ensure models register with SQLModel.metadata
    from . import schema_version

    target = bind if bind is not None else engine
    if production:
        with target.connect() as conn:
            rev = schema_version.check_revision(conn)
        logger.info("init_db: schema at %s (managed by Alembic); no DDL run", rev)
        return

    with target.connect() as conn:
        rev = schema_version.current_revision(conn) if conn.dialect.name == "postgresql" else None
    if rev is not None:
        if rev not in schema_version.COMPATIBLE_REVISIONS:
            logger.warning("init_db: schema at %s, which this release does not list; "
                           "run alembic upgrade %s", rev, schema_version.TARGET_REVISION)
        else:
            logger.info("init_db: schema at %s (managed by Alembic)", rev)
        return

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
