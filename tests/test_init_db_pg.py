"""init_db against a real PostgreSQL holding an older schema and existing data.

Needs TEST_POSTGRES_URL; run through ``scripts/test_pg.sh``. Skips otherwise.

The "older schema" is the current one minus everything init_db is responsible
for adding: the tables created since (``app_instances``,
``feedback_submissions``) and the columns in ``_COLUMN_MIGRATIONS``. Once a
production schema dump exists, that dump replaces this stand-in.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL = os.environ.get("TEST_POSTGRES_URL")

NEW_TABLES = ("app_instances", "feedback_submissions")


def _engine():
    from sqlalchemy import create_engine

    return create_engine(URL)


def _old_schema(engine) -> None:
    from sqlalchemy import text
    from sqlmodel import SQLModel

    import db.models  # noqa: F401
    from db.engine import _COLUMN_MIGRATIONS

    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    SQLModel.metadata.create_all(engine)
    with engine.begin() as c:
        for t in NEW_TABLES:
            c.execute(text(f"DROP TABLE {t}"))
        for table, column, _stmt in _COLUMN_MIGRATIONS:
            c.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        c.execute(text(
            "INSERT INTO users (id, whatsapp_number, angel_api_key, angel_client_id, "
            "angel_password_encrypted, angel_totp_secret_encrypted, is_active, created_at, updated_at) "
            "VALUES (1, '+910000000000', 'k', 'AB1234', 'p', 't', true, now(), now()), "
            "       (2, NULL, 'k2', 'CD5678', 'p2', 't2', false, now(), now())"
        ))
        c.execute(text(
            "INSERT INTO schedules (id, user_id, kind, interval_minutes, next_run, enabled, created_at) "
            "VALUES (1, 1, 'daily_briefing', 1440, now(), true, now())"
        ))


def _tables(engine) -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(engine).get_table_names())


def _columns(engine, table) -> set[str]:
    from sqlalchemy import inspect

    return {c["name"] for c in inspect(engine).get_columns(table)}


def _snapshot(engine):
    from sqlalchemy import text

    with engine.connect() as c:
        users = c.execute(text(
            "SELECT id, whatsapp_number, angel_client_id, angel_password_encrypted, is_active "
            "FROM users ORDER BY id")).all()
        schedules = c.execute(text("SELECT id, user_id, interval_minutes FROM schedules ORDER BY id")).all()
    return users, schedules


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_upgrades_an_older_schema_and_keeps_data():
    from db.engine import init_db

    engine = _engine()
    _old_schema(engine)
    before = _snapshot(engine)

    cap = _Capture()
    logging.getLogger("db.engine").addHandler(cap)
    logging.getLogger("db.engine").setLevel(logging.INFO)
    try:
        init_db(bind=engine)
        first = [m for m in cap.messages if "added columns" in m]
        cap.messages.clear()
        init_db(bind=engine)
        second = [m for m in cap.messages if "added columns" in m]
    finally:
        logging.getLogger("db.engine").removeHandler(cap)

    assert set(NEW_TABLES) <= _tables(engine)
    from db.engine import _COLUMN_MIGRATIONS

    for table, column, _stmt in _COLUMN_MIGRATIONS:
        assert column in _columns(engine, table), (table, column)
    assert first and "users.app_password_hash" in first[0], first
    assert second == [], "a second run must not ALTER anything"
    assert _snapshot(engine) == before, "existing rows changed"


def test_new_tables_enforce_their_checks():
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from db.engine import init_db

    engine = _engine()
    _old_schema(engine)
    init_db(bind=engine)
    for bad in (("", "react"), ("x" * 1001, "react"), ("ok", "email")):
        try:
            with engine.begin() as c:
                c.execute(text("INSERT INTO feedback_submissions (feedback, channel, created_at) "
                               "VALUES (:f, :ch, now())"), {"f": bad[0], "ch": bad[1]})
        except IntegrityError:
            continue
        raise AssertionError(f"accepted invalid feedback row {bad[1]!r}")


def test_lock_wait_fails_cleanly_and_applies_nothing():
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from db.engine import init_db

    engine = _engine()
    _old_schema(engine)

    holder = engine.connect()
    tx = holder.begin()
    holder.execute(text("LOCK TABLE users IN ACCESS SHARE MODE"))
    try:
        started = time.monotonic()
        try:
            init_db(bind=engine, lock_timeout="1s")
        except OperationalError as e:
            assert "lock timeout" in str(e).lower(), e
        else:
            raise AssertionError("init_db should have hit the lock timeout")
        assert time.monotonic() - started < 10
    finally:
        tx.rollback()
        holder.close()

    assert not set(NEW_TABLES) & _tables(engine), "a failed init_db left new tables behind"
    assert "app_password_hash" not in _columns(engine, "users")


def test_up_to_date_schema_takes_no_lock_on_existing_tables():
    from sqlalchemy import text

    from db.engine import init_db

    engine = _engine()
    _old_schema(engine)
    init_db(bind=engine)

    holder = engine.connect()
    tx = holder.begin()
    holder.execute(text("LOCK TABLE users IN ACCESS SHARE MODE"))
    holder.execute(text("LOCK TABLE schedules IN ACCESS SHARE MODE"))
    done = threading.Event()
    errors = []

    def run():
        try:
            init_db(bind=engine, lock_timeout="2s")
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)
        done.set()

    threading.Thread(target=run).start()
    try:
        assert done.wait(10), "init_db hung"
        assert not errors, errors
    finally:
        tx.rollback()
        holder.close()


if __name__ == "__main__":
    if not URL:
        print("TEST_POSTGRES_URL not set; skipping (run scripts/test_pg.sh)")
        sys.exit(0)
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
