"""Baseline: the schema as it stood before Alembic.

Revision ID: 20261109_01_baseline
Revises:
Create Date: 2026-11-09

Before this revision the schema was produced by ``SQLModel.metadata.create_all``
plus the additive ALTERs in ``db.engine._LEGACY_COLUMN_MIGRATIONS``. This
revision changes nothing: a database that predates Alembic is *stamped* here by
``db.engine.init_db`` (after those ALTERs have run), and a new database is
created from the models and stamped at head.

The ID is date-prefixed on purpose. The ``staging`` branch carries an
unrelated Alembic history whose IDs start ``0001_``; keeping the two disjoint
means neither can be mistaken for the other when they are reconciled.
"""

revision = "20261109_01_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
