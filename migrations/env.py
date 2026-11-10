"""Alembic environment.

The URL comes from ``db.engine.DATABASE_URL`` unless the caller set
``sqlalchemy.url`` (the tests do). ``db.engine.init_db`` passes its own open
connection in ``config.attributes["connection"]`` so the migration runs inside
the advisory lock it holds.

``render_as_batch`` lets the same revisions run on SQLite (tests, local
experiments), where ``ALTER COLUMN`` means rebuilding the table. On Postgres
batch mode issues plain ``ALTER TABLE`` statements.
"""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool
from sqlmodel import SQLModel

import db.models  # noqa: F401  -- register every table on SQLModel.metadata

config = context.config
target_metadata = SQLModel.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from db.engine import DATABASE_URL

    return DATABASE_URL


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    from db.engine import _connect_args_for

    url = _url()
    engine = create_engine(url, poolclass=pool.NullPool, connect_args=_connect_args_for(url))
    with engine.connect() as conn:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
