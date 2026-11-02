"""Alembic environment: connect, check the database may be migrated, migrate.

The connection comes from, in order: ``config.attributes["connection"]``
(scripts and tests, which own the transaction), ``MIGRATION_DATABASE_URL``,
and outside production ``DATABASE_URL``. In production the URL must be Neon's
direct endpoint: advisory locks and DDL do not belong on the pooler.

:mod:`migrations.guard` decides from the database's own state whether the
command may run. Offline (``--sql``) mode is not supported.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

import db.models  # noqa: F401  -- registers the app tables on SQLModel.metadata
from migrations import guard
from migrations.helpers import APP_TABLES

config = context.config


def _production_env() -> bool:
    from web.security_config import is_production

    return is_production()


def _url() -> str:
    url = (os.environ.get("MIGRATION_DATABASE_URL") or "").strip()
    if _production_env():
        if not url:
            raise guard.MigrationRefused("MIGRATION_DATABASE_URL must be set in production")
        if "-pooler" in (urlsplit(url).hostname or ""):
            raise guard.MigrationRefused("MIGRATION_DATABASE_URL must be the direct endpoint, not the pooler")
        return url
    return url or os.environ.get("DATABASE_URL", "")


def _include_name(name, type_, parent_names) -> bool:
    if type_ == "table":
        return name in APP_TABLES
    return True


def _command() -> str | None:
    fn = context.get_context().opts.get("fn")
    return guard.WRITE_COMMANDS.get(getattr(fn, "__name__", ""))


def _destination() -> str | None:
    # The argument as typed: get_revision_argument() would resolve "head" to
    # a revision id and let a symbolic target through the production rule.
    raw = context.get_context().opts.get("destination_rev")
    return None if raw is None else str(raw)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=SQLModel.metadata,
        include_name=_include_name,
        compare_type=True,
        transaction_per_migration=False,
        on_version_apply=_after_step,
    )
    state = guard.inspect_state(connection)
    guard.check(
        state,
        command=_command(),
        destination=_destination(),
        new_label=context.get_x_argument(as_dictionary=True).get("label"),
        gate_passed=config.attributes.get("baseline_gate_passed") is True,
        production_env=_production_env(),
        release=(os.environ.get("ALEMBIC_PRODUCTION_RELEASE") or "").strip() or None,
    )
    config.attributes["_label"] = state.label
    with context.begin_transaction():
        context.run_migrations()


def _after_step(*, ctx, step, heads, run_args) -> None:
    # Second line of defence: nothing but an upgrade ever runs on production,
    # even if the command could not be identified up front. Raising here
    # rolls back the whole run (one transaction).
    if config.attributes.get("_label") == "production" and (step.is_stamp or not step.is_upgrade):
        raise guard.MigrationRefused("only upgrades run against the production database")


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    from services.logging_config import configure_logging

    configure_logging()
    url = _url()
    # As in db/engine.py: Neon's sslmode=require is forwarded explicitly.
    connect_args = {"sslmode": "require"} if "sslmode=require" in url.lower() else {}
    engine = create_engine(url, poolclass=NullPool, connect_args=connect_args)
    with engine.connect() as conn:
        _run(conn)


if context.is_offline_mode():
    raise guard.MigrationRefused("offline (--sql) migrations are not supported")
run_migrations_online()
