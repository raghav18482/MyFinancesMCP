"""What migration files may use besides ``op`` and ``sa``.

``scripts/check_migrations.py`` lets a revision call these only with a
literal table name from :data:`APP_TABLES`. Each one refuses rather than
destroy data: a downgrade that would lose rows raises
:class:`IrreversibleWithData`.
"""
from __future__ import annotations

from alembic import context, op
from sqlalchemy import text

#: The tables the app owns. Autogenerate and every helper are limited to these.
APP_TABLES: tuple[str, ...] = (
    "users",
    "mcp_tokens",
    "oauth_clients",
    "oauth_grants",
    "oauth_codes",
    "oauth_tokens",
    "schedules",
    "logs",
    "risk_profiles",
    "decision_records",
    "chat_threads",
    "app_instances",
    "feedback_submissions",
    "db_identity",
)

#: Created and migrated by google-adk in the same database. Never touched here.
ADK_TABLES: tuple[str, ...] = ("adk_internal_metadata", "sessions", "events", "app_states", "user_states")

#: Alembic's own bookkeeping.
ALEMBIC_TABLES: tuple[str, ...] = ("alembic_version",)

LABELS: tuple[str, ...] = ("production", "rehearsal", "development", "test")


class IrreversibleWithData(RuntimeError):
    """A downgrade step would delete data, so it refused."""


def _require_app_table(table: str) -> None:
    if table not in APP_TABLES:
        raise ValueError(f"{table!r} is not an app table")


def x_argument(name: str, default: str | None = None) -> str | None:
    """A ``-x name=value`` argument from the alembic command line."""
    return context.get_x_argument(as_dictionary=True).get(name, default)


def drop_table_if_empty(table: str) -> None:
    _require_app_table(table)
    bind = op.get_bind()
    if bind.execute(text(f'SELECT EXISTS (SELECT 1 FROM "{table}")')).scalar():
        raise IrreversibleWithData(f"{table} holds rows; not dropping it")
    op.drop_table(table)


def drop_column_if_all_null(table: str, column: str) -> None:
    _require_app_table(table)
    bind = op.get_bind()
    if bind.execute(text(f'SELECT EXISTS (SELECT 1 FROM "{table}" WHERE "{column}" IS NOT NULL)')).scalar():
        raise IrreversibleWithData(f"{table}.{column} holds values; not dropping it")
    op.drop_column(table, column)
