"""Which database revisions this release's code can run against.

Alembic owns the production schema from R7 on. Every release states:

- ``REQUIRED_REVISION``: the revision its code needs at minimum.
- ``TARGET_REVISION``: the one its pre-deploy step upgrades production to.
  ``alembic upgrade head`` is refused on a production database; the exact
  revision must be named.
- ``COMPATIBLE_REVISIONS``: every revision the code starts against. Production
  startup refuses any other (:func:`check_revision`). A revision newer than
  ``REQUIRED_REVISION`` may appear only if it is ``EXPAND_ONLY`` and was
  reserved in ``docs/db/revision-plan.md`` before this release shipped, so a
  code rollback never meets a schema it cannot run on.
"""
from __future__ import annotations

from sqlalchemy import text

RELEASE_ID = "R7"
REQUIRED_REVISION = "0001a_db_identity"
TARGET_REVISION = "0001a_db_identity"
#: Revisions reserved in docs/db/revision-plan.md, expand-only, that a later
#: release applies. Listing them here lets this release start again after that
#: (a code rollback). Their files do not exist yet in this release.
RESERVED_REVISIONS: tuple[str, ...] = ("0002_delivery_ledger",)
COMPATIBLE_REVISIONS: tuple[str, ...] = ("0001a_db_identity",) + RESERVED_REVISIONS


class IncompatibleSchema(RuntimeError):
    """The database is not at a revision this release can run against."""


def current_revision(conn) -> str | None:
    """The database's Alembic revision, or ``None`` if it has never been stamped."""
    exists = conn.execute(text("SELECT to_regclass('alembic_version') IS NOT NULL")).scalar()
    if not exists:
        return None
    rows = [r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))]
    if len(rows) != 1:
        raise IncompatibleSchema(f"alembic_version holds {len(rows)} rows; expected exactly one")
    return rows[0]


def check_revision(conn) -> str:
    """Return the revision, or raise :class:`IncompatibleSchema` if it is not listed."""
    rev = current_revision(conn)
    if rev is None:
        raise IncompatibleSchema(
            "the database has no Alembic revision; stamp the baseline first "
            "(scripts/db_stamp_baseline.py, docs/db/revision-plan.md)"
        )
    if rev not in COMPATIBLE_REVISIONS:
        raise IncompatibleSchema(
            f"database revision {rev} is not one release {RELEASE_ID} runs against "
            f"({', '.join(COMPATIBLE_REVISIONS)})"
        )
    return rev
