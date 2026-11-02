"""The delivery ledger on a real PostgreSQL: constraints, transitions, races, the pipeline.

Needs TEST_POSTGRES_URL; run through ``scripts/test_pg.sh``. Skips otherwise.
Every test starts from a schema built by the real migrations (``alembic
upgrade`` to this release's target revision), so the constraints and the
trigger are the production ones.

The guarantees under test: one row per schedule slot; nothing is sent without
a ``sending`` row committed first; ``unknown`` is never retried; only certain
not-sent attempts are, at most three in all and only within three hours of the
slot; one manual send in flight per user.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import random
import sys
import threading
import time
from datetime import timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

URL = os.environ.get("TEST_POSTGRES_URL")
_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from sqlalchemy import create_engine

        _ENGINE = create_engine(URL, pool_size=60, max_overflow=0)
    return _ENGINE


def _reset():
    from alembic import command
    from sqlalchemy import text

    import db_stamp_baseline as st

    engine = _engine()
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    from db import schema_version

    with engine.connect() as c:
        # This release's revision: the models select every column it adds.
        command.upgrade(st.alembic_config(c, label="test"), schema_version.TARGET_REVISION)
        c.commit()
    with engine.begin() as c:
        for uid in (1, 2):
            c.execute(text(
                "INSERT INTO users (id, whatsapp_number, angel_api_key, angel_client_id, "
                "angel_password_encrypted, angel_totp_secret_encrypted, is_active, created_at, updated_at) "
                "VALUES (:id, :wa, 'k', :cid, 'p', 't', true, now(), now())"
            ), {"id": uid, "wa": f"+91000000000{uid}", "cid": f"AB123{uid}"})
    return engine


def _add_schedule(engine, *, ago="1 minute", user=1, interval=1440, enabled=True) -> int:
    from sqlalchemy import text

    with engine.begin() as c:
        return c.execute(text(
            f"INSERT INTO schedules (user_id, kind, interval_minutes, next_run, enabled, status, created_at) "
            f"VALUES (:u, 'daily_briefing', :iv, clock_timestamp() - interval '{ago}', :en, 'pending', now()) "
            f"RETURNING id"
        ), {"u": user, "iv": interval, "en": enabled}).scalar_one()


def _q(engine, sql, **params):
    from sqlalchemy import text

    with engine.connect() as c:
        return c.execute(text(sql), params).first()


def _x(engine, sql, **params):
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text(sql), params)


def _delivery(engine, did):
    return _q(engine, "SELECT * FROM briefing_deliveries WHERE id = :id", id=did)


@contextlib.contextmanager
def _ledger(engine):
    from services.schedular import ledger

    with mock.patch.object(ledger, "engine", engine):
        yield ledger


@contextlib.contextmanager
def _pipeline(engine, *, post=None, briefing_delay=0.0, fail_brief=False):
    """job_executor and the ledger on the test DB; login, briefing and WhatsApp faked."""
    from sqlmodel import Session

    from services.schedular import job_executor, ledger, whatsapp

    sent = []

    async def fake_post(phone, message):
        sent.append(phone)
        if post is not None:
            return await post()
        return 200

    async def fake_brief(_sid):
        if fail_brief:
            raise RuntimeError("market data unavailable")
        if briefing_delay:
            await asyncio.sleep(briefing_delay)
        return "briefing text"

    with mock.patch.object(ledger, "engine", engine), \
            mock.patch.object(job_executor, "get_session", lambda: Session(engine)), \
            mock.patch.object(job_executor, "decrypt_value", lambda v: v), \
            mock.patch.object(job_executor, "sessions", mock.Mock()), \
            mock.patch.object(job_executor, "generate_daily_briefing", fake_brief), \
            mock.patch.object(whatsapp, "_post", fake_post):
        yield sent


def _run(claim):
    from services.schedular import job_executor

    asyncio.run(job_executor.run_one(claim))


def _expire(engine, did):
    _x(engine, "UPDATE briefing_deliveries SET lease_expires_at = clock_timestamp() - interval '1 second' "
               "WHERE id = :id", id=did)


def _not_sent(reason):
    from services.schedular import whatsapp

    async def post():
        raise whatsapp.DeliveryNotSent(reason, "test", 503 if reason == "not_ready" else None)
    return post


def _unknown():
    from services.schedular import whatsapp

    async def post():
        raise whatsapp.DeliveryUnknown("http_5xx", "test", 500)
    return post


# ── constraints ──────────────────────────────────────────────────────────


def _violates(engine, sql, constraint):
    from sqlalchemy.exc import IntegrityError

    try:
        _x(engine, sql)
    except IntegrityError as e:
        assert constraint in str(e), (constraint, str(e).splitlines()[0])
        return
    raise AssertionError(f"accepted: {sql}")


def test_each_constraint_refuses_its_impossible_state():
    engine = _reset()
    sid = _add_schedule(engine)
    cols = "(kind, schedule_id, user_id, slot_at, idempotency_key, status, attempts, claim_token, lease_expires_at, accepted_at)"
    key = "'sched:1:20261102T030000Z'"
    lease = "'tok', now() + interval '1 minute'"
    cases = [
        (f"('scheduled', NULL, 1, now(), {key}, 'pending', 0, {lease}, NULL)", "ck_bd_schedule"),
        (f"('manual', {sid}, 1, now(), 'manual:1:{'a' * 32}', 'pending', 0, {lease}, NULL)", "ck_bd_schedule"),
        (f"('scheduled', {sid}, 1, now(), {key}, 'pending', 0, NULL, NULL, NULL)", "ck_bd_lease"),
        (f"('scheduled', {sid}, 1, now(), {key}, 'accepted', 1, NULL, NULL, NULL)", "ck_bd_accepted"),
        (f"('scheduled', {sid}, 1, now(), {key}, 'failed_final', 4, NULL, NULL, NULL)", "ck_bd_attempts"),
        (f"('scheduled', {sid}, 1, now(), 'sched:1:yesterday', 'failed_final', 1, NULL, NULL, NULL)", "ck_bd_key"),
        (f"('scheduled', {sid}, 1, now(), {key}, 'sending', 0, {lease}, NULL)", "ck_bd_sending"),
        (f"('scheduled', {sid}, 1, now(), {key}, 'delivered', 1, NULL, NULL, NULL)", "ck_bd_status"),
        (f"('manual', NULL, 1, now(), 'manual:1:{'b' * 32}', 'not_sent_retryable', 1, NULL, NULL, NULL)", "ck_bd_retry"),
    ]
    for values, constraint in cases:
        _violates(engine, f"INSERT INTO briefing_deliveries {cols} VALUES {values}", constraint)

    slot = "TIMESTAMPTZ '2026-11-02 03:00:00+00'"
    _x(engine, f"INSERT INTO briefing_deliveries {cols} VALUES "
               f"('scheduled', {sid}, 1, {slot}, {key}, 'failed_final', 1, NULL, NULL, NULL)")
    _violates(engine, f"INSERT INTO briefing_deliveries {cols} VALUES "
                      f"('scheduled', {sid}, 1, {slot}, 'sched:1:20261102T030001Z', 'failed_final', 1, NULL, NULL, NULL)",
              "uq_bd_sched_slot")
    _x(engine, f"INSERT INTO briefing_deliveries {cols} VALUES "
               f"('manual', NULL, 1, now(), 'manual:1:{'c' * 32}', 'pending', 0, {lease}, NULL)")
    _violates(engine, f"INSERT INTO briefing_deliveries {cols} VALUES "
                      f"('manual', NULL, 1, now(), 'manual:1:{'d' * 32}', 'sending', 1, {lease}, NULL)",
              "uq_bd_manual_inflight")


def test_the_trigger_versions_every_update():
    engine = _reset()
    _add_schedule(engine)
    with _ledger(engine) as ledger:
        claim = ledger.claim_next_scheduled()
    before = _delivery(engine, claim.id)
    _x(engine, "UPDATE briefing_deliveries SET outcome_reason = 'connect' WHERE id = :id", id=claim.id)
    after = _delivery(engine, claim.id)
    assert after.row_version == before.row_version + 1 and after.updated_at > before.updated_at


# ── claiming ─────────────────────────────────────────────────────────────


def test_claim_inserts_the_row_and_rearms_the_schedule_atomically():
    from sqlalchemy import text

    engine = _reset()
    sid = _add_schedule(engine, ago="5 minutes")
    slot = _q(engine, "SELECT next_run FROM schedules WHERE id = :id", id=sid).next_run
    with _ledger(engine) as ledger:
        with mock.patch.object(ledger, "_REARM_SCHEDULE", text("SELECT 1/0")):
            try:
                ledger.claim_next_scheduled()
            except Exception:
                pass
        assert _q(engine, "SELECT count(*) AS n FROM briefing_deliveries").n == 0, "insert rolled back"
        assert _q(engine, "SELECT next_run FROM schedules WHERE id = :id", id=sid).next_run == slot
        claim = ledger.claim_next_scheduled()
    row = _delivery(engine, claim.id)
    assert row.status == "pending" and row.idempotency_key == f"sched:{sid}:{slot:%Y%m%dT%H%M%SZ}"
    sched = _q(engine, "SELECT next_run, status FROM schedules WHERE id = :id", id=sid)
    assert sched.status == "done" and sched.next_run == slot + timedelta(days=1), "same time tomorrow"
    with _ledger(engine) as ledger:
        assert ledger.claim_next_scheduled() is None


def test_a_slot_that_already_has_a_row_is_not_claimed_again():
    engine = _reset()
    sid = _add_schedule(engine)
    with _ledger(engine) as ledger:
        first = ledger.claim_next_scheduled()
        # As if an older release left the schedule armed at the same slot.
        _x(engine, "UPDATE schedules SET next_run = :slot WHERE id = :id", slot=first.slot_at, id=sid)
        assert ledger.claim_next_scheduled() is None
    assert _q(engine, "SELECT count(*) AS n FROM briefing_deliveries").n == 1


def test_twenty_workers_one_slot_one_claim_and_fifty_over_twenty_each_once():
    engine = _reset()
    _add_schedule(engine)

    def race(n, fn):
        out, barrier = [], threading.Barrier(n)

        def worker():
            barrier.wait()
            while True:
                c = fn()
                if c is None:
                    return
                out.append(c.schedule_id)
        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return out

    with _ledger(engine) as ledger:
        assert len(race(20, ledger.claim_next_scheduled)) == 1
        engine = _reset()
        ids = [_add_schedule(engine, ago=f"{i + 1} minutes") for i in range(20)]
        got = race(50, ledger.claim_next_scheduled)
    assert sorted(got) == sorted(ids)
    assert _q(engine, "SELECT count(*) AS n FROM briefing_deliveries").n == 20


def test_a_lease_that_expires_during_a_lock_wait_cannot_be_renewed():
    from sqlalchemy import text

    engine = _reset()
    _add_schedule(engine)
    with _ledger(engine) as ledger:
        claim = ledger.claim_next_scheduled(lease_seconds=1)
        result = {}
        with engine.connect() as holder:
            tx = holder.begin()
            holder.execute(text("SELECT id FROM briefing_deliveries WHERE id = :id FOR UPDATE"), {"id": claim.id})
            t = threading.Thread(target=lambda: result.setdefault("ok", ledger.renew(claim)))
            t.start()
            time.sleep(2.0)
            tx.commit()
            t.join()
        assert result["ok"] is False
        assert ledger.mark_sending(claim) is None


# ── the pipeline, end to end ─────────────────────────────────────────────


def test_accepted_path_marks_sending_first_and_logs():
    engine = _reset()
    sid = _add_schedule(engine)
    seen = []

    async def post():
        seen.append(_q(engine, "SELECT status FROM briefing_deliveries").status)
        return 200

    with _pipeline(engine, post=post) as sent:
        from services.schedular import ledger
        claim = ledger.claim_next_scheduled()
        _run(claim)
    row = _delivery(engine, claim.id)
    assert seen == ["sending"], "sending is committed before the request"
    assert len(sent) == 1 and row.status == "accepted" and row.attempts == 1 and row.http_status == 200
    assert row.accepted_at is not None and row.claim_token is None
    assert _q(engine, "SELECT status FROM logs WHERE schedule_id = :id", id=sid).status == "success"


def test_two_fifty_nine_late_sends_and_three_oh_one_is_skipped():
    for ago, sends, status in (("2 hours 59 minutes", 1, "accepted"), ("3 hours 1 minute", 0, "skipped_stale")):
        engine = _reset()
        _add_schedule(engine, ago=ago)
        with _pipeline(engine) as sent:
            from services.schedular import ledger
            claim = ledger.claim_next_scheduled()
            _run(claim)
        assert len(sent) == sends and _delivery(engine, claim.id).status == status, ago


def test_not_ready_is_retried_with_backoff_up_to_three_attempts():
    engine = _reset()
    _add_schedule(engine)
    with _pipeline(engine, post=_not_sent("not_ready")) as sent:
        from services.schedular import ledger
        claim = ledger.claim_next_scheduled()
        _run(claim)
        row = _delivery(engine, claim.id)
        assert row.status == "not_sent_retryable" and row.attempts == 1 and row.outcome_reason == "not_ready"
        wait = _q(engine, "SELECT next_attempt_at - clock_timestamp() AS w FROM briefing_deliveries").w
        assert timedelta(minutes=4) < wait <= timedelta(minutes=5)
        assert ledger.claim_next_retry() is None, "not before the backoff"
        for attempt in (2, 3):
            _x(engine, "UPDATE briefing_deliveries SET next_attempt_at = clock_timestamp()")
            retry = ledger.claim_next_retry()
            assert retry is not None and retry.attempts == attempt - 1
            _run(retry)
        row = _delivery(engine, claim.id)
        assert len(sent) == 3 and row.attempts == 3 and row.status == "failed_final", row.status
        _x(engine, "UPDATE briefing_deliveries SET next_attempt_at = clock_timestamp()")
        assert ledger.claim_next_retry() is None


def test_a_retry_that_succeeds():
    engine = _reset()
    _add_schedule(engine)
    outcomes = [_not_sent("connect"), None]

    async def post():
        step = outcomes.pop(0)
        return await step() if step else 200

    with _pipeline(engine, post=post):
        from services.schedular import ledger
        _run(ledger.claim_next_scheduled())
        _x(engine, "UPDATE briefing_deliveries SET next_attempt_at = clock_timestamp()")
        _run(ledger.claim_next_retry())
    row = _q(engine, "SELECT * FROM briefing_deliveries")
    assert row.status == "accepted" and row.attempts == 2


def test_unknown_and_certain_failures_are_never_retried():
    for post, status in ((_unknown(), "unknown"), (_not_sent("http_4xx"), "failed_final"),
                         (_not_sent("auth"), "failed_final"), (_not_sent("bad_phone"), "failed_final")):
        engine = _reset()
        _add_schedule(engine)
        with _pipeline(engine, post=post) as sent:
            from services.schedular import ledger
            _run(ledger.claim_next_scheduled())
            _x(engine, "UPDATE briefing_deliveries SET next_attempt_at = clock_timestamp()")
            assert ledger.claim_next_retry() is None
            ledger.reap()
        row = _q(engine, "SELECT * FROM briefing_deliveries")
        assert len(sent) == 1 and row.status == status, (status, row.status)


def test_no_retry_beyond_the_window():
    engine = _reset()
    _add_schedule(engine, ago="2 hours 58 minutes")
    with _pipeline(engine, post=_not_sent("not_ready")):
        from services.schedular import ledger
        claim = ledger.claim_next_scheduled()
        _run(claim)
    # The 5-minute backoff would land after slot + 3 h: no retry left.
    assert _delivery(engine, claim.id).status == "failed_final"


def test_generation_failure_retries_scheduled_slots_and_fails_manual_ones():
    engine = _reset()
    _add_schedule(engine)
    with _pipeline(engine, fail_brief=True) as sent:
        from services.schedular import ledger
        sched = ledger.claim_next_scheduled()
        _run(sched)
        manual = ledger.start_manual(2)
        _run(manual)
    assert not sent
    s, m = _delivery(engine, sched.id), _delivery(engine, manual.id)
    assert s.status == "not_sent_retryable" and s.attempts == 0 and s.outcome_reason == "generation"
    assert m.status == "failed_final" and m.outcome_reason == "generation"


def test_a_schedule_disabled_mid_run_is_not_sent():
    engine = _reset()
    sid = _add_schedule(engine)

    with _pipeline(engine, briefing_delay=0.3) as sent:
        from services.schedular import ledger
        claim = ledger.claim_next_scheduled()
        threading.Timer(0.1, lambda: _x(engine, "UPDATE schedules SET enabled = false WHERE id = :id", id=sid)).start()
        _run(claim)
    assert not sent
    row = _delivery(engine, claim.id)
    assert row.status == "failed_final" and row.outcome_reason == "disabled"


def test_a_crash_after_acceptance_is_reaped_unknown_and_never_resent():
    engine = _reset()
    sid = _add_schedule(engine)
    with _pipeline(engine) as sent:
        from services.schedular import ledger
        claim = ledger.claim_next_scheduled()
        with mock.patch.object(ledger, "record_outcome", side_effect=SystemExit("process died")):
            try:
                _run(claim)
            except SystemExit:
                pass
        assert _delivery(engine, claim.id).status == "sending"
        _expire(engine, claim.id)
        assert ledger.reap() == [(claim.id, "unknown")]
        _x(engine, "UPDATE briefing_deliveries SET next_attempt_at = clock_timestamp()")
        assert ledger.claim_next_retry() is None and ledger.claim_next_scheduled() is None
    assert len(sent) == 1
    assert _q(engine, "SELECT next_run > clock_timestamp() AS armed FROM schedules WHERE id = :id", id=sid).armed


def test_a_late_acceptance_is_recorded_as_evidence():
    engine = _reset()
    _add_schedule(engine)

    async def slow_post():
        _expire(engine, claim_box[0].id)
        from services.schedular import ledger
        ledger.reap()
        return 200

    claim_box = []
    with _pipeline(engine, post=slow_post):
        from services.schedular import ledger
        claim_box.append(ledger.claim_next_scheduled())
        _run(claim_box[0])
    row = _delivery(engine, claim_box[0].id)
    assert row.status == "accepted" and row.accepted_at is not None


def test_the_heartbeat_keeps_a_long_run_alive_and_its_absence_loses_it():
    from services.schedular import job_executor

    for beat, expected in ((0.2, "accepted"), (60, None)):
        engine = _reset()
        _add_schedule(engine)
        with _pipeline(engine, briefing_delay=1.5), mock.patch.object(job_executor, "HEARTBEAT_SECONDS", beat):
            from services.schedular import ledger
            claim = ledger.claim_next_scheduled(lease_seconds=1)
            _run(claim)
            row = _delivery(engine, claim.id)
            if expected:
                assert row.status == expected, row.status
            else:
                assert row.status == "pending", "lease lapsed, nothing sent"
                assert ledger.reap() == [(claim.id, "not_sent_retryable")]


# ── the reaper against the retry claimer ─────────────────────────────────


def test_reaper_and_retry_claimer_race_on_boundary_rows():
    engine = _reset()
    _add_schedule(engine)
    with _ledger(engine) as ledger:
        base = ledger.claim_next_scheduled()
        _x(engine, "UPDATE briefing_deliveries SET status = 'not_sent_retryable', attempts = 1, "
                   "claim_token = NULL, lease_expires_at = NULL, next_attempt_at = clock_timestamp() "
                   "WHERE id = :id", id=base.id)
        outcomes = {"claimed": 0, "reaped": 0, "neither": 0}
        for _ in range(200):
            # The 3 h window ends within a few milliseconds of now, either side.
            offset_ms = random.randint(-15, 15)
            _x(engine, "UPDATE briefing_deliveries SET status = 'not_sent_retryable', claim_token = NULL, "
                       "lease_expires_at = NULL, next_attempt_at = clock_timestamp() - interval '1 second', "
                       f"slot_at = clock_timestamp() - interval '3 hours' + interval '{offset_ms} milliseconds' "
                       "WHERE id = :id", id=base.id)
            got, barrier = {}, threading.Barrier(2)

            def claimer():
                barrier.wait()
                got["claim"] = ledger.claim_next_retry()

            def reaper():
                barrier.wait()
                got["reaped"] = ledger.reap()

            ts = [threading.Thread(target=claimer), threading.Thread(target=reaper)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            row = _delivery(engine, base.id)
            claimed, reaped = got["claim"] is not None, bool(got["reaped"])
            assert not (claimed and reaped), "both acted on one row"
            assert row.attempts <= 3
            if claimed:
                assert row.status == "pending" and row.claim_token == got["claim"].claim_token
                outcomes["claimed"] += 1
            elif reaped:
                assert row.status == "failed_final"
                outcomes["reaped"] += 1
            else:
                outcomes["neither"] += 1
    assert outcomes["claimed"] and outcomes["reaped"], outcomes


def test_reaper_closes_expired_pending_and_spent_retries():
    engine = _reset()
    sid = _add_schedule(engine)
    with _ledger(engine) as ledger:
        claim = ledger.claim_next_scheduled()
        _expire(engine, claim.id)
        assert ledger.reap() == [(claim.id, "not_sent_retryable")], "never reached sending"
        _x(engine, "UPDATE schedules SET enabled = false WHERE id = :id", id=sid)
        assert ledger.reap() == [(claim.id, "failed_final")]
        manual = ledger.start_manual(2)
        _expire(engine, manual.id)
        assert ledger.reap() == [(manual.id, "failed_final")]
    from sqlalchemy import text

    with engine.connect() as c:
        statuses = [r[0] for r in c.execute(text("SELECT status FROM logs ORDER BY id"))]
    assert statuses == ["retrying", "failed", "failed"], statuses


# ── manual sends ─────────────────────────────────────────────────────────


def test_twenty_concurrent_manual_sends_one_wins():
    engine = _reset()
    results, barrier = [], threading.Barrier(20)
    with _ledger(engine) as ledger:
        def go():
            barrier.wait()
            results.append(ledger.start_manual(1))
        ts = [threading.Thread(target=go) for _ in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    assert sum(r is not None for r in results) == 1
    assert _q(engine, "SELECT count(*) AS n FROM briefing_deliveries").n == 1


def test_a_failed_manual_insert_leaves_no_cooldown():
    from sqlalchemy import text

    engine = _reset()
    with _ledger(engine) as ledger:
        with mock.patch.object(ledger, "_INSERT_MANUAL_PG", text("SELECT 1/0")):
            try:
                ledger.start_manual(1)
            except Exception:
                pass
        assert _q(engine, "SELECT last_manual_briefing_at FROM users WHERE id = 1").last_manual_briefing_at is None
        assert ledger.start_manual(1) is not None
        assert ledger.start_manual(1) is None, "cooldown"
        assert ledger.start_manual(2) is not None, "per user"


def test_manual_and_scheduled_sends_are_independent():
    engine = _reset()
    _add_schedule(engine)
    with _pipeline(engine) as sent:
        from services.schedular import ledger
        manual = ledger.start_manual(1)
        sched = ledger.claim_next_scheduled()
        _run(manual)
        _run(sched)
    assert len(sent) == 2
    assert {_delivery(engine, manual.id).status, _delivery(engine, sched.id).status} == {"accepted"}


# ── deletion ─────────────────────────────────────────────────────────────


def test_schedules_with_deliveries_cannot_be_hard_deleted_but_accounts_can():
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session

    from services import account_deletion

    engine = _reset()
    sid = _add_schedule(engine)
    with _pipeline(engine):
        from services.schedular import ledger
        _run(ledger.claim_next_scheduled())
        ledger.start_manual(1)
    _x(engine, "DELETE FROM logs")  # logs reference schedules too; isolate the ledger's key
    try:
        _x(engine, "DELETE FROM schedules WHERE id = :id", id=sid)
    except IntegrityError as e:
        assert "briefing_deliveries" in str(e)
    else:
        raise AssertionError("a schedule with deliveries was deleted")
    with mock.patch.object(account_deletion, "get_session", lambda: Session(engine)):
        report = account_deletion.delete_user(1)
    assert report.deliveries == 2
    assert _q(engine, "SELECT count(*) AS n FROM briefing_deliveries").n == 0
    assert _q(engine, "SELECT count(*) AS n FROM users WHERE id = 1").n == 0


if __name__ == "__main__":
    if not URL:
        print("TEST_POSTGRES_URL not set; skipped (run scripts/test_pg.sh)")
        sys.exit(0)
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed, 0 skipped")
