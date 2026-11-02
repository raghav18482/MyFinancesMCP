"""Compare app-table schemas: database with database, database with models.

Only the app tables (:data:`BASELINE_TABLES`) are compared; ADK's and
Alembic's tables are ignored. Read-only.
"""
from __future__ import annotations

from migrations.helpers import APP_TABLES, POST_BASELINE_TABLES

#: The tables the baseline is about: everything but what later revisions create.
BASELINE_TABLES = tuple(t for t in APP_TABLES if t not in POST_BASELINE_TABLES)


def schema_diff(conn_a, conn_b, tables: tuple[str, ...] = BASELINE_TABLES) -> list[str]:
    from migrations.fingerprint import catalog, diff

    return diff(catalog(conn_a, tables), catalog(conn_b, tables))


def model_drift(conn) -> list[str]:
    """Differences between the database and ``db/models.py``, as alembic sees them."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    import db.models  # noqa: F401

    def include_name(name, type_, parent_names):
        return name in BASELINE_TABLES if type_ == "table" else True

    mc = MigrationContext.configure(conn, opts={"compare_type": True, "include_name": include_name})
    return sorted(_describe(d) for d in compare_metadata(mc, SQLModel.metadata))


def _describe(d) -> str:
    """One line per alembic autogenerate difference."""
    if isinstance(d, list):  # column-level changes arrive grouped
        return "; ".join(_describe(x) for x in d)
    kind = d[0]
    if kind.startswith("modify_"):
        _schema, table, column, _existing, old, new = d[1:7]
        return f"{kind} {table}.{column}: database={old!r} models={new!r}"
    obj = d[-1]
    table = getattr(obj, "table", None)
    if kind.endswith("_column"):
        return f"{kind} {d[2]}.{obj.name}"
    if table is not None:
        return f"{kind} {table.name}.{obj.name}"
    return f"{kind} {getattr(obj, 'name', obj)}"
