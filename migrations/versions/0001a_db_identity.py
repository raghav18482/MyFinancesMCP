"""Which database this is: one guarded row in ``db_identity``.

Revision ID: 0001a_db_identity
Revises: 0001_baseline
Create Date: 2026-11-02

``migrations/env.py`` reads the label to decide what may run. Triggers keep the
row from being deleted, truncated, duplicated or quietly relabelled:

- INSERT needs ``app.db_identity_init`` set to the new id in the same
  transaction (only this revision does that).
- UPDATE may change only ``label``, never to ``production``, and only with
  ``app.db_identity_relabel`` set to ``<id>:<old>:<new>`` (``scripts/db_relabel.py``).
- DELETE and TRUNCATE always fail.

This stops accidents, not someone with SQL access who means it: that needs
database roles (optional hardening, see docs/db/revision-plan.md).

Run with ``-x label=<production|rehearsal|development|test>``. ``production``
is accepted only from scripts/db_stamp_baseline.py.
"""
import uuid

from alembic import op
import sqlalchemy as sa

from migrations import helpers

revision = "0001a_db_identity"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

#: True when the previous release's code can run against this revision.
EXPAND_ONLY = True


def upgrade() -> None:
    label = helpers.x_argument("label")
    if label not in helpers.LABELS:
        raise ValueError("run with -x label=production|rehearsal|development|test")
    new_id = str(uuid.uuid4())

    op.execute(
        """
        CREATE TABLE db_identity (
            singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            id         uuid NOT NULL UNIQUE,
            label      text NOT NULL CHECK (label IN ('production', 'rehearsal', 'development', 'test')),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION db_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
                RAISE EXCEPTION 'db_identity rows cannot be deleted';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF current_setting('app.db_identity_init', true) IS DISTINCT FROM NEW.id::text THEN
                    RAISE EXCEPTION 'db_identity is written only by its migration';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.singleton IS DISTINCT FROM OLD.singleton
               OR NEW.id IS DISTINCT FROM OLD.id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'only db_identity.label may change';
            END IF;
            IF NEW.label IS DISTINCT FROM OLD.label THEN
                IF NEW.label = 'production' THEN
                    RAISE EXCEPTION 'a database is labelled production only by the baseline stamp';
                END IF;
                IF current_setting('app.db_identity_relabel', true) IS DISTINCT FROM
                   OLD.id::text || ':' || OLD.label || ':' || NEW.label THEN
                    RAISE EXCEPTION 'relabel through scripts/db_relabel.py';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER db_identity_guard_row
            BEFORE INSERT OR UPDATE OR DELETE ON db_identity
            FOR EACH ROW EXECUTE FUNCTION db_identity_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER db_identity_guard_truncate
            BEFORE TRUNCATE ON db_identity
            FOR EACH STATEMENT EXECUTE FUNCTION db_identity_guard()
        """
    )
    op.execute(sa.text("SELECT set_config('app.db_identity_init', :id, true)").bindparams(id=new_id))
    op.execute(
        sa.text("INSERT INTO db_identity (id, label) VALUES (CAST(:id AS uuid), :label)")
        .bindparams(id=new_id, label=label)
    )
    op.execute(sa.text("SELECT set_config('app.db_identity_init', '', true)"))


def downgrade() -> None:
    # Refused on a production database by migrations/env.py.
    op.execute("DROP TABLE db_identity")
    op.execute("DROP FUNCTION db_identity_guard()")
