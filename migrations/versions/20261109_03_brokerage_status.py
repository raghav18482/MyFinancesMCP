"""Add users.brokerage_status and backfill it.

Revision ID: 20261109_03_brokerage_status
Revises: 20261109_02_broker_nullable
Create Date: 2026-11-09

New column, NOT NULL with server default ``'not_connected'`` and a CHECK on the
six allowed values (``db.models.BrokerageStatus``).

Backfill: a row is ``connected`` when all four Angel One credentials are
present and non-empty, otherwise ``not_connected``. "Valid" cannot be proven
here: a migration has neither the encryption key nor any business calling the
broker. Credentials that turn out to be stale are moved to ``expired`` by the
app the first time the broker rejects them, and the user reconnects.

No credential, password hash or id is read or written beyond that test.

Downgrade drops the constraint and the column; the credentials are untouched.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261109_03_brokerage_status"
down_revision = "20261109_02_broker_nullable"
branch_labels = None
depends_on = None

_STATUSES = ("not_connected", "pending", "connected", "expired", "disconnected", "failed")
_CHECK = "brokerage_status IN ({})".format(",".join(f"'{s}'" for s in _STATUSES))
_CREDENTIALS = (
    "angel_api_key",
    "angel_client_id",
    "angel_password_encrypted",
    "angel_totp_secret_encrypted",
)


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "brokerage_status",
                sa.String(),
                nullable=False,
                server_default="not_connected",
            )
        )
        batch.create_check_constraint("ck_users_brokerage_status", _CHECK)

    present = " AND ".join(f"{c} IS NOT NULL AND {c} <> ''" for c in _CREDENTIALS)
    op.execute(f"UPDATE users SET brokerage_status = 'connected' WHERE {present}")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("ck_users_brokerage_status", type_="check")
        batch.drop_column("brokerage_status")
