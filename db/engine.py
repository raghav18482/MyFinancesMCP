"""Shared SQLModel engine + session factory + bootstrap.

One process-wide ``engine`` reads ``DATABASE_URL`` from env (Postgres by
default). ``init_db()`` brings the database to the current Alembic revision and
is safe to run on every startup.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql+psycopg://my_finance:admin@localhost/my_finance"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)

# Neon (and other SSL-required hosts) embed sslmode=require in the URL.
# psycopg v3 needs it forwarded explicitly via connect_args when using the
# pooler endpoint. Passing an empty dict is harmless for local connections.
def _connect_args_for(url: str) -> dict:
    return {"sslmode": "require"} if "sslmode=require" in url.lower() else {}


engine = create_engine(
    DATABASE_URL, echo=False, pool_pre_ping=True, connect_args=_connect_args_for(DATABASE_URL)
)


# The additive ALTERs that kept the schema current before Alembic. They now run
# only once per database, on its first start under this release, to bring a
# pre-Alembic database exactly to the baseline revision before it is stamped.
# Do not add to this list: schema changes are Alembic revisions now.
_LEGACY_COLUMN_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'pending'",
    # No default: NULL means "this user has not set an app password yet", which
    # the login path treats as "cannot log in, re-enroll to set one".
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS app_password_hash VARCHAR",
    # Logout bumps this; cookies issued with an older value stop working.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0",
)


#: The revision a pre-Alembic database is stamped at. Must match the first
#: file in ``migrations/versions``.
BASELINE_REVISION = "20261109_01_baseline"

# Arbitrary, fixed key for ``pg_advisory_xact_lock``: several workers starting
# together migrate one at a time, and the rest find the work already done.
_MIGRATION_LOCK_KEY = 7_120_261_109

_ALEMBIC_INI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alembic.ini")


def _alembic_config(connection):
    from alembic.config import Config

    cfg = Config(_ALEMBIC_INI)
    cfg.attributes["connection"] = connection
    return cfg


def _current_revision(connection) -> str | None:
    from alembic.runtime.migration import MigrationContext

    return MigrationContext.configure(connection).get_current_revision()


def migrate(connection, *, auto_migrate: bool = True) -> str:
    """Bring the database behind ``connection`` to the head revision.

    Three cases, decided by what is in the database:

    - **Empty:** create every table from the models, stamp head.
    - **Tables but no revision** (created before Alembic): run the legacy
      ALTERs to reach the baseline, create any missing tables, stamp the
      baseline, then upgrade — which is where the ``users`` changes land.
    - **Versioned:** create any missing tables, upgrade.

    With ``auto_migrate`` false, an existing database is left at whatever
    revision it is at (run ``alembic upgrade head`` yourself). Returns a short
    description of what was done, for the log.
    """
    from alembic import command

    from . import models  # noqa: F401  -- ensure models register with SQLModel.metadata

    cfg = _alembic_config(connection)
    current = _current_revision(connection)
    has_users = inspect(connection).has_table("users")

    if current is not None:
        from alembic.script import ScriptDirectory

        known = {rev.revision for rev in ScriptDirectory.from_config(cfg).walk_revisions()}
        if current not in known:
            # Most likely the ``staging`` branch's separate Alembic history
            # (``0001_baseline`` ... ``0003_broker_connections``). Guessing how
            # to reconcile two histories is not this function's job.
            raise RuntimeError(
                f"Database is at Alembic revision {current!r}, which this release does "
                f"not know (it knows {sorted(known)}). It was migrated by another branch; "
                "reconcile the two migration histories before deploying this one."
            )

    if current is None and not has_users:
        SQLModel.metadata.create_all(connection)
        command.stamp(cfg, "head")
        return "created schema and stamped head"

    if not auto_migrate:
        return f"left at revision {current or 'none (pre-Alembic)'}; DB_AUTO_MIGRATE=0"

    if current is None:
        # Missing tables first (the ALTERs below name ``schedules``), then the
        # ALTERs, which are no-ops wherever a column already exists.
        SQLModel.metadata.create_all(connection)
        if connection.dialect.name == "postgresql":
            for stmt in _LEGACY_COLUMN_MIGRATIONS:
                connection.execute(text(stmt))
        command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, "head")
        return f"stamped {BASELINE_REVISION} and upgraded to head"

    SQLModel.metadata.create_all(connection)
    command.upgrade(cfg, "head")
    return f"upgraded from {current} to head"


def init_db() -> None:
    """Bring the database to the current schema; see :func:`migrate`.

    Runs in a single transaction under an advisory lock on Postgres, so a
    failure leaves the database exactly as it was (Postgres DDL is
    transactional) and concurrent workers never migrate twice. Raises on
    failure: a half-known schema is worse than not starting.
    """
    auto = os.environ.get("DB_AUTO_MIGRATE", "1").strip() != "0"
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _MIGRATION_LOCK_KEY})
        outcome = migrate(conn, auto_migrate=auto)
    logger.info("init_db: %s on %s", outcome, _safe_url(DATABASE_URL))


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
