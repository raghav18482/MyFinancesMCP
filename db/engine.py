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


# Lightweight per-column migrations applied on every startup. Each entry is
# idempotent (``ADD COLUMN IF NOT EXISTS``) so it is safe to run repeatedly
# until Alembic is introduced.
_COLUMN_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'pending'",
)


def init_db() -> None:
    """Create all SQLModel tables and apply additive column migrations.

    Idempotent; replace with Alembic when schema evolves beyond simple adds.
    """
    from . import models  # noqa: F401  -- ensure models register with SQLModel.metadata

    SQLModel.metadata.create_all(engine)

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            for stmt in _COLUMN_MIGRATIONS:
                conn.execute(text(stmt))

    logger.info("init_db: tables ensured on %s", _safe_url(DATABASE_URL))


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
