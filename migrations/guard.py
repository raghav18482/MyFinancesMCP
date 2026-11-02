"""Whether an alembic command may run against this database.

The database's own state decides, not a flag or the URL:

========================================  =========================================
State                                     Rule
========================================  =========================================
No app tables, no ``alembic_version``     Allowed only outside a production
                                          environment.
App tables, no ``alembic_version``        Refused, unless the baseline stamp script
                                          has passed its gate in this process.
``db_identity.label = 'production'``      Needs ``ALEMBIC_PRODUCTION_RELEASE`` equal
                                          to ``RELEASE_ID`` and the exact
                                          ``TARGET_REVISION`` (``head`` is refused).
                                          Downgrade and stamp: always refused.
Any other label                           Normal.
========================================  =========================================

Read-only commands (``current``, ``history``) are always allowed. Labelling a
database ``production`` (``-x label=production``) is allowed only from the
stamp script, after its gate.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from db import schema_version
from migrations.helpers import APP_TABLES

#: alembic.command's inner function names for the commands that write.
WRITE_COMMANDS = {"upgrade": "upgrade", "downgrade": "downgrade", "do_stamp": "stamp"}


class MigrationRefused(RuntimeError):
    """This command must not run against this database."""


@dataclass(frozen=True)
class DbState:
    app_tables: frozenset[str]
    has_version: bool
    label: str | None

    @property
    def empty(self) -> bool:
        return not self.app_tables and not self.has_version


def inspect_state(conn) -> DbState:
    present = {
        r[0] for r in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ))
    }
    label = None
    if "db_identity" in present:
        label = conn.execute(text("SELECT label FROM db_identity")).scalar()
    return DbState(
        app_tables=frozenset(present & set(APP_TABLES)),
        has_version="alembic_version" in present,
        label=label,
    )


def check(
    state: DbState,
    *,
    command: str | None,
    destination: str | None,
    new_label: str | None,
    gate_passed: bool,
    production_env: bool,
    release: str | None,
) -> None:
    """Raise :class:`MigrationRefused` unless ``command`` may run. ``command`` is
    ``upgrade``, ``downgrade``, ``stamp``, or ``None`` for a read-only one."""
    if command is None:
        return
    if new_label == "production" and not gate_passed:
        raise MigrationRefused("only scripts/db_stamp_baseline.py may label a database production")

    if state.label == "production":
        if command != "upgrade":
            raise MigrationRefused(f"{command} is never run against the production database")
        if release != schema_version.RELEASE_ID:
            raise MigrationRefused(
                "production: set ALEMBIC_PRODUCTION_RELEASE to this release's RELEASE_ID"
            )
        if destination != schema_version.TARGET_REVISION:
            raise MigrationRefused(
                f"production: upgrade to exactly {schema_version.TARGET_REVISION}, not {destination!r}"
            )
        return

    if state.empty:
        if production_env:
            raise MigrationRefused("refusing to build a schema on an empty database in production")
        return

    if not state.has_version and not gate_passed:
        raise MigrationRefused(
            "this database has tables but no Alembic revision; it must be stamped "
            "through scripts/db_stamp_baseline.py, which checks it matches the baseline"
        )
