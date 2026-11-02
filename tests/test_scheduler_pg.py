"""The scheduler tick and the schedule-level helpers left beside the ledger.

Needs TEST_POSTGRES_URL for the database tests (``scripts/test_pg.sh``); the
``next_slot`` and CLI tests run without it. Claims, sends, retries and leases
are covered in ``tests/test_delivery_ledger_pg.py``.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from services.schedular import repository as repo  # noqa: E402

URL = os.environ.get("TEST_POSTGRES_URL")


def test_next_slot_is_anchored_to_the_schedule():
    t0 = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)  # 08:30 IST
    day = 1440
    assert repo.next_slot(t0, day, t0 + timedelta(minutes=5)) == t0 + timedelta(days=1)
    assert repo.next_slot(t0, day, t0) == t0 + timedelta(days=1)
    assert repo.next_slot(t0, day, t0 + timedelta(days=2, hours=1)) == t0 + timedelta(days=3)
    assert repo.next_slot(t0, day, t0 - timedelta(hours=1)) == t0 + timedelta(days=1)
    assert repo.next_slot(t0, 60, t0 + timedelta(minutes=61)) == t0 + timedelta(hours=2)
    # A run finishing late does not drift the time of day.
    late = repo.next_slot(t0, day, t0 + timedelta(minutes=47, seconds=13))
    assert (late.hour, late.minute, late.second) == (3, 0, 0)


def test_the_cli_is_refused_in_production():
    from services.schedular import briefing_delivery

    for env in ({"APP_ENV": "production"}, {"RENDER": "true"}):
        with mock.patch.dict(os.environ, env):
            try:
                briefing_delivery.cli_claim()
            except RuntimeError:
                continue
            raise AssertionError(env)
    with mock.patch.dict(os.environ, {"APP_ENV": "development", "RENDER": ""}):
        assert briefing_delivery.cli_claim().source == "cli"


def test_rows_r5_left_running_are_reclaimed_never_resent():
    from sqlalchemy import text

    from test_delivery_ledger_pg import _add_schedule, _q, _reset, _x

    engine = _reset()
    expired = _add_schedule(engine, ago="10 minutes")
    no_lease_old = _add_schedule(engine, ago="7 hours")
    no_lease_recent = _add_schedule(engine, ago="5 hours")
    _x(engine, "UPDATE schedules SET status = 'running' WHERE id IN (:a, :b, :c)",
       a=expired, b=no_lease_old, c=no_lease_recent)
    _x(engine, "UPDATE schedules SET claim_token = 'r5', lease_expires_at = clock_timestamp() - interval '1 second' "
               "WHERE id = :id", id=expired)
    got = sorted(repo.reclaim_expired(bind=engine))
    assert got == sorted([(expired, 1), (no_lease_old, 1)]), got
    for sid in (expired, no_lease_old):
        row = _q(engine, "SELECT status, next_run > clock_timestamp() AS armed FROM schedules WHERE id = :id", id=sid)
        assert row.status == "unknown" and row.armed
    assert _q(engine, "SELECT status FROM schedules WHERE id = :id", id=no_lease_recent).status == "running"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM briefing_deliveries")).scalar_one() == 0, "nothing resent"


def test_one_tick_reaps_retries_first_then_claims_slots():
    from services.schedular import runner

    from test_delivery_ledger_pg import _add_schedule, _pipeline, _q, _reset, _x

    engine = _reset()
    first = _add_schedule(engine, ago="2 minutes")
    order = []

    async def post():
        order.append(_q(engine, "SELECT schedule_id, attempts FROM briefing_deliveries "
                                "WHERE status = 'sending'"))
        return 200

    with _pipeline(engine, post=post), mock.patch.object(repo, "engine", engine):
        from services.schedular import ledger
        claim = ledger.claim_next_scheduled()
        # Left as a due retry by an earlier tick.
        _x(engine, "UPDATE briefing_deliveries SET status = 'not_sent_retryable', attempts = 1, claim_token = NULL, "
                   "lease_expires_at = NULL, next_attempt_at = clock_timestamp() WHERE id = :id", id=claim.id)
        second = _add_schedule(engine, ago="1 minute")
        asyncio.run(runner.check_and_run_jobs())
    assert [(r.schedule_id, r.attempts) for r in order] == [(first, 2), (second, 1)], order
    assert _q(engine, "SELECT count(*) AS n FROM briefing_deliveries WHERE status = 'accepted'").n == 2


if __name__ == "__main__":
    passed = skipped = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        if URL is None and name not in ("test_next_slot_is_anchored_to_the_schedule",
                                        "test_the_cli_is_refused_in_production"):
            print(f"  skip {name} (no TEST_POSTGRES_URL)")
            skipped += 1
            continue
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed} tests passed, {skipped} skipped")
