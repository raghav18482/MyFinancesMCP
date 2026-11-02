"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Only what scripts/check_migrations.py allows: the op calls on its list, and
raw SQL as reviewed inline literals (migrations/raw_sql_review.json).
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}
from migrations import helpers

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

#: True when the previous release's code can run against this revision.
EXPAND_ONLY = True


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
