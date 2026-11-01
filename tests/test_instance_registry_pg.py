"""Single-instance detection (services.instance_registry).

The alert timing tests need no database. The upsert, count and prune tests
need TEST_POSTGRES_URL and are skipped without it; run ``scripts/test_pg.sh``.
"""
from __future__ import annotations

import logging
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import instance_registry as ir  # noqa: E402

URL = os.environ.get("TEST_POSTGRES_URL")


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.count = 0

    def emit(self, record):
        if "MULTIPLE_INSTANCES_DETECTED" in record.getMessage():
            self.count += 1


def _monitor(clock):
    class _NoDb:
        class dialect:
            name = "sqlite"

    return ir.InstanceMonitor(_NoDb(), "test", clock=clock)


def _with_errors(fn):
    h = _Errors()
    logging.getLogger(ir.__name__).addHandler(h)
    try:
        fn(h)
    finally:
        logging.getLogger(ir.__name__).removeHandler(h)


def test_deploy_overlap_does_not_alert():
    clock = _Clock()
    m = _monitor(clock)

    def check(h):
        for minute in range(6):  # five minutes of two instances, then one
            clock.t = minute * 60
            m.evaluate(2 if minute < 5 else 1)
        assert h.count == 0

    _with_errors(check)


def test_sustained_second_instance_alerts_once_per_hour():
    clock = _Clock()
    m = _monitor(clock)

    def check(h):
        for minute in range(0, 16):
            clock.t = minute * 60
            m.evaluate(2)
        assert h.count == 1, "alerts once after 15 minutes"
        for minute in range(16, 75):
            clock.t = minute * 60
            m.evaluate(2)
        assert h.count == 1, "not again within the hour"
        clock.t = 76 * 60
        m.evaluate(2)
        assert h.count == 2

    _with_errors(check)


def test_returning_to_one_instance_resets_the_window():
    clock = _Clock()
    m = _monitor(clock)

    def check(h):
        for minute in range(10):
            clock.t = minute * 60
            m.evaluate(2)
        clock.t = 600
        m.evaluate(1)
        for minute in range(11, 20):
            clock.t = minute * 60
            m.evaluate(2)
        assert h.count == 0, "the 15 minutes restart after a single-instance beat"

    _with_errors(check)


def _pg_engine():
    from sqlalchemy import create_engine, text
    from sqlmodel import SQLModel

    import db.models  # noqa: F401

    engine = create_engine(URL)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    SQLModel.metadata.create_all(engine)
    return engine


def test_pg_concurrent_beats_count_every_live_instance():
    if not URL:
        return
    engine = _pg_engine()
    monitors = [ir.InstanceMonitor(engine, f"host:{i}:x") for i in range(4)]
    errors = []

    def beat(m):
        try:
            for _ in range(5):
                m.beat()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=beat, args=(m,)) for m in monitors]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert monitors[0].beat() == 4


def test_pg_stale_rows_are_pruned_and_stop_removes_self():
    if not URL:
        return
    from sqlalchemy import text

    engine = _pg_engine()
    with engine.begin() as c:
        c.execute(text("INSERT INTO app_instances VALUES "
                       "('old:1:x', now() - interval '3 days', now() - interval '2 days'), "
                       "('recent:1:x', now() - interval '10 minutes', now() - interval '10 minutes')"))
    m = ir.InstanceMonitor(engine, "me:1:x")
    assert m.beat() == 1, "the 10-minute-old row is not live; the 2-day-old one is pruned"
    with engine.connect() as c:
        ids = {r[0] for r in c.execute(text("SELECT instance_id FROM app_instances"))}
    assert ids == {"recent:1:x", "me:1:x"}, ids
    m.stop()
    with engine.connect() as c:
        ids = {r[0] for r in c.execute(text("SELECT instance_id FROM app_instances"))}
    assert ids == {"recent:1:x"}, ids


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed" + ("" if URL else " (PostgreSQL cases skipped)"))
