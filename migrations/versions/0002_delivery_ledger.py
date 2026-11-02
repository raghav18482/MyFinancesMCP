"""Delivery ledger: one row per briefing send, scheduled or manual.

Revision ID: 0002_delivery_ledger
Revises: 0001a_db_identity
Create Date: 2026-11-02

``briefing_deliveries`` records each slot's (or manual request's) delivery and
every state it passes through (services/schedular/ledger.py). The constraints
make the impossible states unrepresentable: a lease exactly while pending or
sending, ``accepted_at`` exactly when accepted, retries only for scheduled
rows, at most three attempts, one row per schedule slot, one manual send in
flight per user. A trigger bumps ``row_version`` and ``updated_at`` on every
update.

Expand-only: a new table nothing older reads. Foreign keys are ``RESTRICT``:
schedules are never hard-deleted, and account deletion removes deliveries first.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migrations import helpers

revision = "0002_delivery_ledger"
down_revision = "0001a_db_identity"
branch_labels = None
depends_on = None

#: True when the previous release's code can run against this revision.
EXPAND_ONLY = True


def upgrade() -> None:
    op.create_table(
        "briefing_deliveries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("schedules.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("slot_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("claim_token", sa.VARCHAR(length=32), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("accepted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("kind IN ('scheduled', 'manual')", name="ck_bd_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'accepted', 'not_sent_retryable', 'failed_final', "
            "'unknown', 'skipped_stale')",
            name="ck_bd_status",
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_bd_attempts"),
        sa.CheckConstraint(
            "outcome_reason IS NULL OR outcome_reason IN ('not_ready', 'connect', 'deadline', 'http_4xx', "
            "'auth', 'bad_phone', 'generation', 'http_5xx', 'http_503', 'http_other', 'transport', "
            "'inconsistent', 'stale', 'disabled', 'lease_expired')",
            name="ck_bd_reason",
        ),
        sa.CheckConstraint("http_status IS NULL OR http_status BETWEEN 100 AND 599", name="ck_bd_http_status"),
        sa.CheckConstraint("(kind = 'scheduled') = (schedule_id IS NOT NULL)", name="ck_bd_schedule"),
        sa.CheckConstraint(
            "(status IN ('pending', 'sending')) = (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_bd_lease",
        ),
        sa.CheckConstraint("(status = 'accepted') = (accepted_at IS NOT NULL)", name="ck_bd_accepted"),
        sa.CheckConstraint(
            "status <> 'not_sent_retryable' OR (kind = 'scheduled' AND next_attempt_at IS NOT NULL)",
            name="ck_bd_retry",
        ),
        sa.CheckConstraint("status <> 'sending' OR attempts >= 1", name="ck_bd_sending"),
        sa.CheckConstraint(
            "idempotency_key ~ '^(sched:[0-9]+:[0-9]{8}T[0-9]{6}Z|manual:[0-9]+:[0-9a-f]{32})$'",
            name="ck_bd_key",
        ),
    )
    op.create_index(
        "uq_bd_sched_slot", "briefing_deliveries", ["schedule_id", "slot_at"], unique=True,
        postgresql_where=sa.text("kind = 'scheduled'"),
    )
    op.create_index(
        "uq_bd_manual_inflight", "briefing_deliveries", ["user_id"], unique=True,
        postgresql_where=sa.text("kind = 'manual' AND status IN ('pending', 'sending')"),
    )
    op.create_index("ix_bd_retry", "briefing_deliveries", ["status", "next_attempt_at"])
    op.create_index("ix_bd_lease", "briefing_deliveries", ["status", "lease_expires_at"])
    op.create_index("ix_bd_user", "briefing_deliveries", ["user_id"])
    op.execute(
        """
        CREATE FUNCTION briefing_deliveries_touch() RETURNS trigger LANGUAGE plpgsql AS $$
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
        CREATE TRIGGER briefing_deliveries_touch
            BEFORE UPDATE ON briefing_deliveries
            FOR EACH ROW EXECUTE FUNCTION briefing_deliveries_touch()
        """
    )


def downgrade() -> None:
    # Refused on production by migrations/env.py, and here if any row exists.
    helpers.drop_table_if_empty("briefing_deliveries")
    op.execute("DROP FUNCTION briefing_deliveries_touch()")
