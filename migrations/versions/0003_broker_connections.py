"""Broker connections, and the users columns the new sign-in will need.

Revision ID: 0003_broker_connections
Revises: 0002_delivery_ledger
Create Date: 2026-11-02

``broker_connections`` will hold a user's Angel One link (account id and the
three encrypted secrets) apart from the user row, so signing in to the app no
longer depends on the broker. Nothing reads or writes it yet: the backfill
and the dual-write code come in the next phase. The database checks shape
only; decryption is checked by the app and the backfill's verification.

- ``broker_account_id`` is upper-case and trimmed and must match
  ``^[A-Z0-9]{4,16}$``. **Gate:** ``scripts/check_broker_ids.py`` must show
  every production client code passing before this revision runs.
- The secrets must look like Fernet tokens (``gAAAAA`` is Fernet's version
  byte and timestamp), 100 to 4096 characters.
- ``created_by = 'backfill'`` exactly when a ``backfill_run_id`` is set.
- A trigger bumps ``row_version`` and ``updated_at`` on every update.

``users`` gains ``firebase_uid``, ``auth_phone_e164``, ``email`` and
``last_login_at``: nullable, no default (a metadata-only change), with checks
and partial unique indexes that every existing row (all NULL) passes.

Expand-only: previous code ignores the new table and columns. Deleting a user
deletes their connections (``ON DELETE CASCADE``).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migrations import helpers

revision = "0003_broker_connections"
down_revision = "0002_delivery_ledger"
branch_labels = None
depends_on = None

#: True when the previous release's code can run against this revision.
EXPAND_ONLY = True


def upgrade() -> None:
    # users is touched while the app runs: never queue behind a long transaction.
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.create_table(
        "broker_connections",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("broker_account_id", sa.Text(), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("totp_secret_encrypted", sa.Text(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_by", sa.Text(), nullable=False, server_default=sa.text("'app'")),
        sa.Column("backfill_run_id", postgresql.UUID(), nullable=True),
        sa.Column("last_verified_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("broker IN ('angelone')", name="ck_bc_broker"),
        sa.CheckConstraint(
            "broker_account_id = upper(btrim(broker_account_id)) AND broker_account_id ~ '^[A-Z0-9]{4,16}$'",
            name="ck_bc_account_id",
        ),
        sa.CheckConstraint(
            "api_key_encrypted ~ '^gAAAAA[A-Za-z0-9_-]+={0,2}$' AND length(api_key_encrypted) BETWEEN 100 AND 4096",
            name="ck_bc_api_key",
        ),
        sa.CheckConstraint(
            "password_encrypted ~ '^gAAAAA[A-Za-z0-9_-]+={0,2}$' AND length(password_encrypted) BETWEEN 100 AND 4096",
            name="ck_bc_password",
        ),
        sa.CheckConstraint(
            "totp_secret_encrypted ~ '^gAAAAA[A-Za-z0-9_-]+={0,2}$' "
            "AND length(totp_secret_encrypted) BETWEEN 100 AND 4096",
            name="ck_bc_totp",
        ),
        sa.CheckConstraint("key_version >= 1", name="ck_bc_key_version"),
        sa.CheckConstraint("status IN ('active', 'needs_reauth', 'disconnected')", name="ck_bc_status"),
        sa.CheckConstraint("created_by IN ('app', 'backfill')", name="ck_bc_created_by"),
        sa.CheckConstraint("(created_by = 'backfill') = (backfill_run_id IS NOT NULL)", name="ck_bc_backfill_run"),
        sa.UniqueConstraint("user_id", "broker", name="uq_bc_user_broker"),
        sa.UniqueConstraint("broker", "broker_account_id", name="uq_bc_account"),
    )
    op.execute(
        """
        CREATE FUNCTION broker_connections_touch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.row_version := OLD.row_version + 1;
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER broker_connections_touch
            BEFORE UPDATE ON broker_connections
            FOR EACH ROW EXECUTE FUNCTION broker_connections_touch()
        """
    )

    op.add_column("users", sa.Column("firebase_uid", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("auth_phone_e164", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("email", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("last_login_at", postgresql.TIMESTAMP(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_users_firebase_uid", "users", "firebase_uid IS NULL OR length(firebase_uid) BETWEEN 1 AND 128",
    )
    op.create_check_constraint(
        "ck_users_auth_phone", "users", "auth_phone_e164 IS NULL OR auth_phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'",
    )
    op.create_index(
        "uq_users_firebase_uid", "users", ["firebase_uid"], unique=True,
        postgresql_where=sa.text("firebase_uid IS NOT NULL"),
    )
    op.create_index(
        "uq_users_auth_phone", "users", ["auth_phone_e164"], unique=True,
        postgresql_where=sa.text("auth_phone_e164 IS NOT NULL"),
    )


def downgrade() -> None:
    # Refused on production by migrations/env.py, and here if any data exists.
    helpers.drop_column_if_all_null("users", "last_login_at")
    helpers.drop_column_if_all_null("users", "email")
    helpers.drop_column_if_all_null("users", "auth_phone_e164")
    helpers.drop_column_if_all_null("users", "firebase_uid")
    helpers.drop_table_if_empty("broker_connections")
    op.execute("DROP FUNCTION broker_connections_touch()")
