"""Tests for the Alembic revisions and the startup path that applies them.

A *legacy* ``users`` table — exactly the shape ``create_all`` plus the old
startup ALTERs produced before Alembic — is built by hand, filled with the
kinds of rows production holds, and taken through ``db.engine.migrate`` (what
``init_db`` runs) and back down again.

SQLite always. Postgres too when ``MIGRATION_TEST_PG_URL`` names a server
(``postgresql+psycopg://user:pass@host/anydb``): a throwaway database is
created there for the test and dropped afterwards; the database in the URL is
only used to issue ``CREATE``/``DROP DATABASE``.

Run directly (``python tests/test_migrations.py``) or under pytest.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from db.engine import BASELINE_REVISION, _alembic_config, _current_revision, migrate  # noqa: E402

HEAD = "20261109_03_brokerage_status"
BROKER_COLUMNS = ("angel_api_key", "angel_client_id", "angel_password_encrypted",
                  "angel_totp_secret_encrypted")

# The users table as production has it before this release.
LEGACY_USERS = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    whatsapp_number VARCHAR UNIQUE,
    angel_api_key VARCHAR NOT NULL,
    angel_client_id VARCHAR NOT NULL UNIQUE,
    angel_password_encrypted VARCHAR NOT NULL,
    angel_totp_secret_encrypted VARCHAR NOT NULL,
    angel_access_token VARCHAR,
    app_password_hash VARCHAR,
    is_active BOOLEAN NOT NULL,
    session_version INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
)
"""
LEGACY_ROWS = [
    # A normal enrolled user.
    (7, "+919000000001", "KEY1", "AA1111", "gAAAAApin1", "gAAAAAtotp1", "tok-1", "scrypt$1", True, 4),
    # Premium registration: credentials but no app password yet.
    (8, "+919000000002", "KEY2", "BB2222", "gAAAAApin2", "gAAAAAtotp2", None, None, True, 0),
    # A row whose PIN was blanked somewhere along the way: not connectable.
    (9, "+919000000003", "KEY3", "CC3333", "", "gAAAAAtotp3", None, "scrypt$3", True, 1),
    # Deactivated, with full credentials.
    (10, None, "KEY4", "DD4444", "gAAAAApin4", "gAAAAAtotp4", None, "scrypt$4", False, 2),
]
INSERT = text(
    "INSERT INTO users (id, whatsapp_number, angel_api_key, angel_client_id, "
    "angel_password_encrypted, angel_totp_secret_encrypted, angel_access_token, "
    "app_password_hash, is_active, session_version, created_at, updated_at) VALUES "
    "(:id, :wa, :key, :cid, :pin, :totp, :tok, :hash, :active, :sv, "
    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
)
SNAPSHOT = text(
    "SELECT id, whatsapp_number, angel_api_key, angel_client_id, angel_password_encrypted, "
    "angel_totp_secret_encrypted, angel_access_token, app_password_hash, is_active, "
    "session_version FROM users ORDER BY id"
)


@contextlib.contextmanager
def sqlite_engine():
    tmp = tempfile.TemporaryDirectory()
    try:
        yield create_engine(f"sqlite:///{os.path.join(tmp.name, 'm.db')}")
    finally:
        tmp.cleanup()


@contextlib.contextmanager
def postgres_engine():
    admin_url = os.environ["MIGRATION_TEST_PG_URL"]
    name = f"migration_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(admin_url.rsplit("/", 1)[0] + f"/{name}")
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _engines():
    yield "sqlite", sqlite_engine
    if os.environ.get("MIGRATION_TEST_PG_URL"):
        yield "postgres", postgres_engine


def _legacy(engine):
    with engine.begin() as c:
        c.execute(text(LEGACY_USERS))
        for r in LEGACY_ROWS:
            c.execute(INSERT, dict(zip(("id", "wa", "key", "cid", "pin", "totp", "tok", "hash",
                                        "active", "sv"), r)))
        return [tuple(row) for row in c.execute(SNAPSHOT)]


def _nullable(conn, column):
    return {c["name"]: c["nullable"] for c in inspect(conn).get_columns("users")}[column]


def _statuses(conn):
    return dict(conn.execute(text("SELECT id, brokerage_status FROM users")).all())


def _insert_app_only(conn, uid=20):
    conn.execute(text(
        "INSERT INTO users (id, whatsapp_number, is_active, session_version, created_at, updated_at) "
        "VALUES (:id, '+919000000020', :t, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
    ), {"id": uid, "t": True})


def test_legacy_upgrade_keeps_every_row_and_backfills_status():
    for name, make in _engines():
        with make() as engine:
            before = _legacy(engine)
            with engine.begin() as c:
                outcome = migrate(c)
            with engine.begin() as c:
                after = [tuple(r) for r in c.execute(SNAPSHOT)]
                statuses = _statuses(c)
                revision = _current_revision(c)
                nullable = {col: _nullable(c, col) for col in BROKER_COLUMNS}

        assert "stamped" in outcome and BASELINE_REVISION in outcome, outcome
        assert revision == HEAD
        assert after == before, f"{name}: ids, hashes, credentials or tokens changed"
        assert statuses == {7: "connected", 8: "connected", 9: "not_connected", 10: "connected"}, statuses
        assert all(nullable.values()), nullable
        print(f"ok  [{name}] legacy upgrade: rows byte-identical, status backfilled, columns nullable")


def test_upgrade_allows_app_only_users_and_enforces_the_status_values():
    for name, make in _engines():
        with make() as engine:
            _legacy(engine)
            with engine.begin() as c:
                migrate(c)
            with engine.begin() as c:
                _insert_app_only(c)
                assert _statuses(c)[20] == "not_connected", "the server default applies"
            try:
                with engine.begin() as c:
                    c.execute(text("UPDATE users SET brokerage_status = 'bogus' WHERE id = 7"))
            except Exception:
                pass
            else:
                raise AssertionError(f"{name}: the CHECK constraint did not reject a bad status")
        print(f"ok  [{name}] users without broker credentials insert; bad statuses are refused")


def test_running_again_is_a_no_op():
    for name, make in _engines():
        with make() as engine:
            before = _legacy(engine)
            with engine.begin() as c:
                migrate(c)
            with engine.begin() as c:
                outcome = migrate(c)
                after = [tuple(r) for r in c.execute(SNAPSHOT)]
        assert outcome.startswith(f"upgraded from {HEAD}"), outcome
        assert after == before
        print(f"ok  [{name}] a second start changes nothing")


def test_downgrade_refuses_while_app_only_users_exist_then_restores():
    from alembic import command

    for name, make in _engines():
        with make() as engine:
            before = _legacy(engine)
            with engine.begin() as c:
                migrate(c)
                _insert_app_only(c)

            # Dropping the status column alone is always possible.
            with engine.begin() as c:
                command.downgrade(_alembic_config(c), "20261109_02_broker_nullable")
                assert "brokerage_status" not in {col["name"] for col in inspect(c).get_columns("users")}

            # Restoring NOT NULL is not, while a user has no credentials. The
            # check runs before any DDL, so nothing is half-done on any backend.
            try:
                with engine.begin() as c:
                    command.downgrade(_alembic_config(c), BASELINE_REVISION)
            except RuntimeError as e:
                assert "no Angel One credentials" in str(e)
            else:
                raise AssertionError(f"{name}: downgrade dropped NOT NULL over NULL rows")

            with engine.begin() as c:
                assert _current_revision(c) == "20261109_02_broker_nullable"
                assert c.execute(text("SELECT count(*) FROM users")).scalar_one() == len(before) + 1
                c.execute(text("DELETE FROM users WHERE id = 20"))

            with engine.begin() as c:
                command.downgrade(_alembic_config(c), BASELINE_REVISION)
            with engine.begin() as c:
                revision = _current_revision(c)
                after = [tuple(r) for r in c.execute(SNAPSHOT)]
                columns = {col["name"] for col in inspect(c).get_columns("users")}
                nullable = {col: _nullable(c, col) for col in BROKER_COLUMNS}

        assert revision == BASELINE_REVISION
        assert after == before, f"{name}: downgrade changed existing rows"
        assert "brokerage_status" not in columns
        assert not any(nullable.values()), nullable
        print(f"ok  [{name}] downgrade refuses over app-only users, then restores the old schema")


def test_fresh_database_is_created_at_head():
    for name, make in _engines():
        with make() as engine:
            with engine.begin() as c:
                outcome = migrate(c)
            with engine.begin() as c:
                revision = _current_revision(c)
                nullable = {col: _nullable(c, col) for col in BROKER_COLUMNS}
                _insert_app_only(c)
            try:
                with engine.begin() as c:
                    c.execute(text("UPDATE users SET brokerage_status = 'bogus'"))
            except Exception:
                pass
            else:
                raise AssertionError(f"{name}: fresh schema lacks the CHECK constraint")
        assert outcome == "created schema and stamped head" and revision == HEAD
        assert all(nullable.values())
        print(f"ok  [{name}] an empty database is created from the models and stamped at head")


def test_a_database_from_another_migration_history_is_refused():
    """A database migrated by the staging branch carries a revision this
    release does not know. Refuse clearly, change nothing."""
    with sqlite_engine() as engine:
        before = _legacy(engine)
        with engine.begin() as c:
            c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            c.execute(text("INSERT INTO alembic_version VALUES ('0003_broker_connections')"))
        try:
            with engine.begin() as c:
                migrate(c)
        except RuntimeError as e:
            assert "0003_broker_connections" in str(e) and "reconcile" in str(e)
        else:
            raise AssertionError("migrated a database from an unknown history")
        with engine.begin() as c:
            after = [tuple(r) for r in c.execute(SNAPSHOT)]
            columns = {col["name"] for col in inspect(c).get_columns("users")}
    assert after == before and "brokerage_status" not in columns
    print("ok  a database from another Alembic history is refused, untouched")


def test_revision_ids_fit_alembic_version():
    """``alembic_version.version_num`` is VARCHAR(32) on Postgres. SQLite does
    not enforce the length, so only this catches an ID that would fail the
    stamp in production."""
    import pathlib
    import re

    versions = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions"
    ids = [re.search(r'^revision = "([^"]+)"', p.read_text(), re.M).group(1)
           for p in versions.glob("*.py")]
    assert ids and all(len(i) <= 32 for i in ids), ids
    assert BASELINE_REVISION in ids and HEAD in ids
    print("ok  every revision id fits in alembic_version.version_num (32 chars)")


def test_auto_migrate_off_leaves_an_existing_database_alone():
    with sqlite_engine() as engine:
        _legacy(engine)
        with engine.begin() as c:
            outcome = migrate(c, auto_migrate=False)
        with engine.begin() as c:
            revision = _current_revision(c)
            columns = {col["name"] for col in inspect(c).get_columns("users")}
    assert "DB_AUTO_MIGRATE=0" in outcome
    assert revision is None and "brokerage_status" not in columns
    print("ok  DB_AUTO_MIGRATE=0 leaves an existing database untouched")


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
    print(f"\n{passed} tests passed")
