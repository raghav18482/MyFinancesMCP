"""Scheduler leases, reclaiming and the manual cooldown on a real PostgreSQL.

Needs TEST_POSTGRES_URL; run through ``scripts/test_pg.sh``. The ``next_slot``
unit tests run without it; the rest are skipped.

The guarantee under test: at most one send attempt per schedule slot. A run
that dies, stalls or loses its lease is reclaimed as ``unknown`` and moves on
to its next slot; nothing is ever resent.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.schedular import repository as repo  # noqa: E402

URL = os.environ.get("TEST_POSTGRES_URL")
_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from sqlalchemy import create_engine

        _ENGINE = create_engine(URL, pool_size=60, max_overflow=0)
    return _ENGINE


def _reset():
    from sqlalchemy import text
    from sqlmodel import SQLModel

    import db.models  # noqa: F401

    engine = _engine()
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    SQLModel.metadata.create_all(engine)
    with engine.begin() as c:
        # Production added this column by ALTER (db.engine._COLUMN_MIGRATIONS),
        # nullable; the model's create_all makes it NOT NULL.
        c.execute(text("ALTER TABLE schedules ALTER COLUMN status DROP NOT NULL"))
        c.execute(text(
            "INSERT INTO users (id, whatsapp_number, angel_api_key, angel_client_id, "
            "angel_password_encrypted, angel_totp_secret_encrypted, is_active, session_version, "
            "created_at, updated_at) "
            "VALUES (1, '+910000000000', 'k', 'AB1234', 'p', 't', true, 0, now(), now())"
        ))
    return engine


def _add_schedule(engine, *, ago: str = "1 minute", status: str | None = "pending",
                  interval: int = 1440, enabled: bool = True, lease: str | None = None) -> int:
    from sqlalchemy import text

    with engine.begin() as c:
        return c.execute(text(
            f"""
            INSERT INTO schedules (user_id, kind, interval_minutes, next_run, enabled, status,
                                   created_at, lease_expires_at, claim_token)
            VALUES (1, 'daily_briefing', :iv, clock_timestamp() - interval '{ago}', :en, :st, now(),
                    {f"clock_timestamp() + interval '{lease}'" if lease else "NULL"},
                    {"'legacy'" if lease else "NULL"})
            RETURNING id
            """
        ), {"iv": interval, "en": enabled, "st": status}).scalar_one()


def _row(engine, sid: int):
    from sqlalchemy import text

    with engine.connect() as c:
        return c.execute(text(
            "SELECT status, next_run, claim_token, lease_expires_at, last_run, "
            "next_run > clock_timestamp() AS in_future FROM schedules WHERE id = :id"
        ), {"id": sid}).first()


def _logs(engine, sid: int) -> list[str]:
    from sqlalchemy import text

    with engine.connect() as c:
        return [r[0] for r in c.execute(
            text("SELECT status FROM logs WHERE schedule_id = :id ORDER BY id"), {"id": sid})]


def _expire(engine, sid: int):
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text("UPDATE schedules SET lease_expires_at = clock_timestamp() - interval '1 second' "
                       "WHERE id = :id"), {"id": sid})


@contextlib.contextmanager
def _pipeline(engine, *, briefing_delay: float = 0.0, post=None):
    """job_executor wired to the test DB, with login, briefing and WhatsApp faked."""
    from sqlmodel import Session

    from services.schedular import job_executor, whatsapp

    sent = []

    async def fake_post(phone, message):
        sent.append((phone, message))
        if post is not None:
            await post()

    async def fake_brief(_sid):
        if briefing_delay:
            await asyncio.sleep(briefing_delay)
        return "briefing text"

    with mock.patch.object(repo, "engine", engine), \
            mock.patch.object(job_executor, "get_session", lambda: Session(engine)), \
            mock.patch.object(job_executor, "decrypt_value", lambda v: v), \
            mock.patch.object(job_executor, "sessions", mock.Mock()), \
            mock.patch.object(job_executor, "generate_daily_briefing", fake_brief), \
            mock.patch.object(whatsapp, "_post", fake_post):
        yield sent


# ── next_slot (no database) ─────────────────────────────────────────────────

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


# ── claims ──────────────────────────────────────────────────────────────────

def test_claim_skips_locked_rows():
    from sqlalchemy import text

    engine = _reset()
    a = _add_schedule(engine, ago="2 minutes")
    b = _add_schedule(engine, ago="1 minute")
    with engine.connect() as holder:
        tx = holder.begin()
        held = holder.execute(repo._CLAIM_SQL).first()
        assert held.id == a, "most overdue first"
        claim = repo.claim_next_due(bind=engine)
        assert claim is not None and claim.id == b
        assert repo.claim_next_due(bind=engine) is None, "the only other row is locked"
        tx.rollback()
    with engine.connect() as c:
        plan = "\n".join(r[0] for r in c.execute(text("EXPLAIN " + str(repo._CLAIM_SQL))))
    assert plan.index("Limit") < plan.index("LockRows"), plan


def test_fifty_workers_claim_each_row_exactly_once():
    engine = _reset()
    ids = {_add_schedule(engine, ago=f"{i + 1} minutes") for i in range(20)}
    claimed, errors = [], []
    barrier = threading.Barrier(50)

    def worker():
        try:
            barrier.wait()
            while True:
                c = repo.claim_next_due(bind=engine)
                if c is None:
                    return
                claimed.append(c.id)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert sorted(claimed) == sorted(ids)


def test_status_null_and_finished_rows_are_claimable_but_running_is_not():
    engine = _reset()
    for st in (None, "pending", "done", "failed", "unknown"):
        _add_schedule(engine, status=st)
    _add_schedule(engine, status="running")
    _add_schedule(engine, enabled=False)
    got = []
    while (c := repo.claim_next_due(bind=engine)) is not None:
        got.append(c.id)
    assert len(got) == 5


# ── leases ──────────────────────────────────────────────────────────────────

def test_a_lease_that_expires_during_a_lock_wait_cannot_be_renewed():
    from sqlalchemy import text

    engine = _reset()
    sid = _add_schedule(engine)
    claim = repo.claim_next_due(lease_seconds=1, bind=engine)
    now_sql = text(str(repo._RENEW_SQL).replace("clock_timestamp()", "now()"))

    def renew_while_locked(renew) -> bool:
        result = {}
        with engine.connect() as holder:
            tx = holder.begin()
            holder.execute(text("SELECT 1 FROM schedules WHERE id = :id FOR UPDATE"), {"id": sid})
            t = threading.Thread(target=lambda: result.setdefault("ok", renew()))
            t.start()
            time.sleep(2.0)  # the lease (1s) expires while renew() waits for the lock
            tx.commit()
            t.join()
        return result["ok"]

    def renew_on_now():
        with engine.begin() as c:
            c.execute(repo._LOCK_SQL, {"id": sid})
            return c.execute(now_sql, {"id": sid, "tok": claim.claim_token, "lease": 600}).first() is not None

    assert renew_while_locked(lambda: repo.renew_lease(claim, bind=engine)) is False
    # Control: the same statement on now() (the transaction's start time)
    # would wrongly extend a lease that has already expired.
    with engine.begin() as c:
        c.execute(text("UPDATE schedules SET lease_expires_at = clock_timestamp() + interval '1 second' "
                       "WHERE id = :id"), {"id": sid})
    assert renew_while_locked(renew_on_now) is True
    # And a finish that waited past the lease records nothing.
    with engine.begin() as c:
        c.execute(text("UPDATE schedules SET lease_expires_at = clock_timestamp() + interval '1 second' "
                       "WHERE id = :id"), {"id": sid})
    assert renew_while_locked(lambda: repo.finish_schedule(claim, "done", bind=engine)) is False


def test_an_expired_lease_can_neither_renew_nor_finish():
    engine = _reset()
    sid = _add_schedule(engine)
    claim = repo.claim_next_due(bind=engine)
    assert repo.renew_lease(claim, bind=engine)
    _expire(engine, sid)
    assert repo.renew_lease(claim, bind=engine) is False
    assert repo.finish_schedule(claim, "done", bind=engine) is False
    assert _row(engine, sid).status == "running", "only the reaper moves it on"
    assert repo.reclaim_expired(bind=engine) == [(sid, 1)]
    row = _row(engine, sid)
    assert row.status == "unknown" and row.in_future and row.claim_token is None
    assert _logs(engine, sid) == ["unknown"]


def test_finish_arms_the_next_slot_at_the_same_time_of_day():
    from sqlalchemy import text

    engine = _reset()
    sid = _add_schedule(engine, ago="40 minutes")
    with engine.connect() as c:
        slot = c.execute(text("SELECT next_run FROM schedules WHERE id = :id"), {"id": sid}).scalar_one()
    claim = repo.claim_next_due(bind=engine)
    assert repo.finish_schedule(claim, "done", bind=engine)
    row = _row(engine, sid)
    assert row.status == "done" and row.claim_token is None and row.lease_expires_at is None
    assert row.next_run == slot + timedelta(days=1)
    assert repo.finish_schedule(claim, "done", bind=engine) is False, "a claim finishes once"


def test_legacy_rows_without_a_lease_are_reclaimed_after_six_hours():
    from sqlalchemy import text

    engine = _reset()
    old = _add_schedule(engine, ago="7 hours", status="running")
    recent = _add_schedule(engine, ago="5 hours", status="running")
    assert repo.reclaim_expired(bind=engine) == [(old, 1)]
    assert _row(engine, old).status == "unknown" and _row(engine, old).in_future
    assert _row(engine, recent).status == "running"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM logs")).scalar_one() == 1


# ── the executor ────────────────────────────────────────────────────────────

def _run(claim):
    from services.schedular import job_executor

    asyncio.run(job_executor.run_one(claim))


def test_a_run_two_fifty_nine_late_sends_once():
    engine = _reset()
    sid = _add_schedule(engine, ago="2 hours 59 minutes")
    assert repo.reclaim_expired(bind=engine) == []
    with _pipeline(engine) as sent:
        claim = repo.claim_next_due(bind=engine)
        assert claim.lateness < repo.STALE_AFTER
        _run(claim)
    assert len(sent) == 1
    assert _row(engine, sid).status == "done" and _row(engine, sid).in_future
    assert _logs(engine, sid) == ["success"]


def test_a_run_three_hours_late_is_skipped():
    engine = _reset()
    sid = _add_schedule(engine, ago="3 hours 1 minute")
    with _pipeline(engine) as sent:
        _run(repo.claim_next_due(bind=engine))
    assert sent == []
    assert _logs(engine, sid) == ["skipped_stale"]
    assert _row(engine, sid).status == "done" and _row(engine, sid).in_future


def test_a_crash_after_acceptance_is_never_resent():
    from services.schedular.briefing_delivery import ScheduleClaim, send_briefing

    engine = _reset()
    sid = _add_schedule(engine)
    with _pipeline(engine) as sent:
        claim = repo.claim_next_due(bind=engine)
        result = asyncio.run(send_briefing(ScheduleClaim(claim), "+910000000000", "x"))
        assert result.accepted
        # The worker dies here, before finish_schedule. Its lease lapses.
        _expire(engine, sid)
        assert repo.reclaim_expired(bind=engine) == [(sid, 1)]
        assert repo.claim_next_due(bind=engine) is None, "the slot is not offered again"
    assert len(sent) == 1
    row = _row(engine, sid)
    assert row.status == "unknown" and row.in_future
    assert _logs(engine, sid) == ["unknown"]


def test_a_stalled_worker_and_the_reaper_never_both_send():
    from services.schedular.briefing_delivery import ABORTED, ScheduleClaim, send_briefing

    engine = _reset()
    # Stalls before its final renewal: the reaper wins, the worker sends nothing.
    sid = _add_schedule(engine)
    with _pipeline(engine) as sent:
        claim = repo.claim_next_due(bind=engine)
        _expire(engine, sid)
        repo.reclaim_expired(bind=engine)
        result = asyncio.run(send_briefing(ScheduleClaim(claim), "+910000000000", "x"))
        assert result.outcome == ABORTED
        assert repo.claim_next_due(bind=engine) is None
    assert sent == []

    # Stalls after its final renewal: it sends late, once; nobody else does.
    sid = _add_schedule(engine)
    with _pipeline(engine) as sent:
        claim = repo.claim_next_due(bind=engine)
        assert repo.renew_lease(claim, bind=engine)
        _expire(engine, sid)
        repo.reclaim_expired(bind=engine)
        with mock.patch.object(repo, "renew_lease", lambda *a, **k: True):  # it passed already
            asyncio.run(send_briefing(ScheduleClaim(claim), "+910000000000", "x"))
        assert repo.claim_next_due(bind=engine) is None
        assert repo.finish_schedule(claim, "done", bind=engine) is False
    assert len(sent) == 1
    assert _row(engine, sid).status == "unknown"


def test_a_schedule_disabled_mid_run_is_not_sent():
    from sqlalchemy import text

    engine = _reset()
    sid = _add_schedule(engine)

    async def disable_during_briefing(_sid):
        with engine.begin() as c:
            c.execute(text("UPDATE schedules SET enabled = false WHERE id = :id"), {"id": sid})
        return "briefing text"

    from services.schedular import job_executor

    with _pipeline(engine) as sent, \
            mock.patch.object(job_executor, "generate_daily_briefing", disable_during_briefing):
        _run(repo.claim_next_due(bind=engine))
    assert sent == []
    assert _logs(engine, sid) == ["skipped"]


def test_the_heartbeat_keeps_a_long_run_alive():
    from services.schedular import job_executor

    def short(fn):
        return lambda claim, **k: fn(claim, lease_seconds=1, bind=engine)

    for heartbeat, expect_sent in ((0.3, 1), (60, 0)):
        engine = _reset()
        sid = _add_schedule(engine)
        with _pipeline(engine, briefing_delay=2.5) as sent, \
                mock.patch.object(repo, "renew_lease", short(repo.renew_lease)), \
                mock.patch.object(job_executor, "HEARTBEAT_SECONDS", heartbeat):
            _run(repo.claim_next_due(lease_seconds=1, bind=engine))
        assert len(sent) == expect_sent, (heartbeat, sent)
        if expect_sent:
            assert _logs(engine, sid) == ["success"]
        else:
            # The lease lapsed during the briefing: nothing sent, the worker
            # cannot finish the schedule, and the reaper moves it on.
            assert _logs(engine, sid) == ["skipped"]
            assert _row(engine, sid).status == "running"
            assert repo.reclaim_expired(bind=engine) == [(sid, 1)]
            assert _row(engine, sid).status == "unknown" and _row(engine, sid).in_future


def test_outcomes_map_to_schedule_status():
    from services.schedular import whatsapp

    cases = (
        (None, "done", ["success"]),
        (whatsapp.DeliveryNotSent("not_ready"), "failed", ["failed"]),
        (whatsapp.DeliveryUnknown("http_5xx"), "unknown", ["unknown"]),
    )
    for exc, status, logs in cases:
        engine = _reset()
        sid = _add_schedule(engine)

        async def post(exc=exc):
            if exc is not None:
                raise exc

        with _pipeline(engine, post=post) as sent:
            _run(repo.claim_next_due(bind=engine))
        assert len(sent) == 1
        assert _row(engine, sid).status == status, exc
        assert _logs(engine, sid) == logs, exc
        assert _row(engine, sid).in_future


# ── manual sends ────────────────────────────────────────────────────────────

def test_twenty_concurrent_manual_sends_one_wins():
    from sqlmodel import Session

    engine = _reset()
    results, barrier = [], threading.Barrier(20)

    def attempt():
        with Session(engine) as s:
            barrier.wait()
            results.append(repo.claim_manual_send(s, 1))

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and results.count(False) == 19


def test_manual_cooldown_expires_and_ignores_inactive_users():
    from sqlalchemy import text
    from sqlmodel import Session

    engine = _reset()
    with Session(engine) as s:
        assert repo.claim_manual_send(s, 1)
        assert not repo.claim_manual_send(s, 1)
        assert repo.claim_manual_send(s, 1, cooldown_seconds=0)
    with engine.begin() as c:
        c.execute(text("UPDATE users SET last_manual_briefing_at = NULL, is_active = false"))
    with Session(engine) as s:
        assert not repo.claim_manual_send(s, 1)


def test_manual_and_scheduled_sends_are_independent():
    from sqlmodel import Session

    from services.schedular import briefing_delivery

    engine = _reset()
    sid = _add_schedule(engine)
    claim = repo.claim_next_due(bind=engine)
    with Session(engine) as s:
        manual = briefing_delivery.start_manual_send(s, 1)
    assert manual is not None, "a running schedule does not block a manual send"
    assert _row(engine, sid).claim_token == claim.claim_token, "and does not touch schedules"
    assert repo.finish_schedule(claim, "done", bind=engine)
    with Session(engine) as s:
        assert briefing_delivery.start_manual_send(s, 1) is None
    assert repo.claim_next_due(bind=engine) is None


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
