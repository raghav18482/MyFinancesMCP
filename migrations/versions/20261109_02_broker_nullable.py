"""Let users exist without Angel One credentials.

Revision ID: 20261109_02_broker_nullable
Revises: 20261109_01_baseline
Create Date: 2026-11-09

Drops NOT NULL from the four Angel One columns so a user can register with a
phone number and password alone. Every existing value is kept as it is; the
unique index on ``angel_client_id`` stays (both Postgres and SQLite allow any
number of NULLs under a unique index).

Downgrade puts NOT NULL back, which is only possible while every row still has
all four values. Once app-only users exist it refuses rather than deleting
them: remove or complete those rows deliberately first.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261109_02_broker_nullable"
down_revision = "20261109_01_baseline"
branch_labels = None
depends_on = None

_COLUMNS = (
    "angel_api_key",
    "angel_client_id",
    "angel_password_encrypted",
    "angel_totp_secret_encrypted",
)


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        for name in _COLUMNS:
            batch.alter_column(name, existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    any_null = " OR ".join(f"{name} IS NULL" for name in _COLUMNS)
    missing = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM users WHERE {any_null}")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"Cannot downgrade: {missing} user(s) have no Angel One credentials, "
            "and these columns cannot become NOT NULL again while they exist. "
            "Delete or complete those rows first; this migration will not do it."
        )
    with op.batch_alter_table("users") as batch:
        for name in _COLUMNS:
            batch.alter_column(name, existing_type=sa.String(), nullable=False)
