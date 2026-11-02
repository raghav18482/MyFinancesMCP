"""The delivery ledger: every briefing send as a row in ``briefing_deliveries``.

Rows and states
---------------
A scheduled slot gets one row, keyed ``sched:<schedule id>:<slot UTC>``; a
manual "send now" gets one, keyed ``manual:<user id>:<random>``. A row moves
through these states, and only these (each UPDATE names the state it expects):

==================  ======================  =====================================
From                To                      When
==================  ======================  =====================================
(none)              pending                 claimed (scheduled) / requested (manual)
pending             skipped_stale           more than 3 h after its slot
pending             not_sent_retryable      generation failed (scheduled)
pending             failed_final            generation failed (manual), or the
                                            schedule was disabled / user deactivated
pending             sending                 committed *before* the HTTP request;
                                            ``attempts + 1``
sending             accepted                WhatsApp accepted it
sending             not_sent_retryable      certainly not sent, the webhook not
                                            ready or unreachable (scheduled, while
                                            attempts and the 3 h window remain)
sending             failed_final            certainly not sent, any other reason,
                                            or no retries left
sending             unknown                 may have been sent (500, timeout,
                                            dropped connection, contradictory reply)
unknown             accepted                the same attempt's 2xx arrived after the
                                            lease expired (evidence, recorded late)
not_sent_retryable  pending                 retry claim: attempts < 3, due, inside
                                            the window, schedule on, user active.
                                            Backoff 5, 15, 45 minutes.
not_sent_retryable  failed_final            reaper: no retries left, window passed,
                                            schedule off or user inactive
pending (expired)   not_sent_retryable /    reaper: never reached ``sending``, so
                    failed_final            nothing went out
sending (expired)   unknown                 reaper
==================  ======================  =====================================

``unknown`` is final for automatic processing and is never retried: the
webhook may have handed the message to WhatsApp before anyone could record it.

Leases and time
---------------
``pending`` and ``sending`` rows carry a ``claim_token`` and a lease. A worker
renews it (heartbeat) and every change it makes requires its token and a live
lease. As in the R5 scheduler, the row lock is taken first and the guarded
UPDATE runs after it, so a lease is judged after any lock wait; every time
comparison uses ``clock_timestamp()``.

A schedule is held only inside the claim transaction: in that transaction its
delivery row is inserted (``ON CONFLICT DO NOTHING`` on the key) and the
schedule is re-armed for its next slot. The delivery row's lease covers the
rest.

PostgreSQL only, except :func:`start_manual`, which also runs on SQLite for the
test harness.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db import engine
from db.models import utcnow

logger = logging.getLogger(__name__)

LEASE_SECONDS = 600
MAX_ATTEMPTS = 3
#: Wait before retry n+1 after n failed attempts.
BACKOFF = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=45))
#: Wait before retrying a slot whose briefing could not be generated.
GENERATION_BACKOFF = timedelta(minutes=15)
#: A slot is not sent, first or retried, later than this after its time.
WINDOW = timedelta(hours=3)
MANUAL_COOLDOWN_SECONDS = 600

#: Certainly-not-sent reasons worth retrying: the webhook was down or not ready.
RETRYABLE_REASONS = frozenset({"not_ready", "connect", "deadline"})
_KNOWN_REASONS = frozenset({
    "not_ready", "connect", "deadline", "http_4xx", "auth", "bad_phone", "generation",
    "http_5xx", "http_503", "http_other", "transport", "inconsistent", "stale", "disabled",
    "lease_expired",
})


def normalize_reason(reason: str) -> str:
    """A webhook reason as the ledger stores it (``outcome_reason`` is checked)."""
    if reason in _KNOWN_REASONS:
        return reason
    if reason.startswith("http_"):
        return "http_other"
    return "transport" if reason else "http_other"


@dataclass(frozen=True)
class DeliveryClaim:
    """A ``pending`` delivery this worker holds the lease on."""

    id: int
    kind: str  # scheduled | manual
    user_id: int
    schedule_id: Optional[int]
    slot_at: datetime
    claim_token: str
    attempts: int  # attempts made before this claim
    claimed_at: datetime

    @property
    def lateness(self) -> timedelta:
        return self.claimed_at - self.slot_at


def _key_for_slot(schedule_id: int, slot_at: datetime) -> str:
    return f"sched:{schedule_id}:{slot_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"


# ── claiming ─────────────────────────────────────────────────────────────

_ANCHORED_NEXT_RUN = (
    "next_run + GREATEST(1, FLOOR(EXTRACT(EPOCH FROM (clock_timestamp() - next_run)) "
    "/ (interval_minutes * 60))::int + 1) * make_interval(mins => interval_minutes)"
)

_DUE_SCHEDULE = text(
    """
    SELECT id, user_id, next_run
      FROM schedules
     WHERE enabled
       AND (status IS NULL OR status IN ('pending', 'done', 'failed', 'unknown'))
       AND next_run <= clock_timestamp()
     ORDER BY next_run
     LIMIT 1
       FOR UPDATE SKIP LOCKED
    """
)

_INSERT_SCHEDULED = text(
    """
    INSERT INTO briefing_deliveries
           (kind, schedule_id, user_id, slot_at, idempotency_key, status,
            claim_token, lease_expires_at)
    VALUES ('scheduled', :sid, :uid, :slot, :key, 'pending',
            :tok, clock_timestamp() + make_interval(secs => :lease))
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING id, attempts, created_at
    """
)

_REARM_SCHEDULE = text(
    f"""
    UPDATE schedules
       SET status = 'done', last_run = clock_timestamp(), next_run = {_ANCHORED_NEXT_RUN},
           claim_token = NULL, lease_expires_at = NULL, claimed_at = NULL
     WHERE id = :sid
    """
)

_DUE_RETRY = text(
    """
    SELECT d.id, d.user_id, d.schedule_id, d.slot_at, d.attempts
      FROM briefing_deliveries d
      JOIN schedules s ON s.id = d.schedule_id
      JOIN users u ON u.id = d.user_id
     WHERE d.status = 'not_sent_retryable'
       AND d.attempts < :max
       AND d.next_attempt_at <= clock_timestamp()
       AND clock_timestamp() < d.slot_at + make_interval(secs => :window)
       AND s.enabled AND u.is_active
     ORDER BY d.next_attempt_at
     LIMIT 1
       FOR UPDATE OF d SKIP LOCKED
    """
)

_RETRY_TO_PENDING = text(
    """
    UPDATE briefing_deliveries
       SET status = 'pending', next_attempt_at = NULL, claim_token = :tok,
           lease_expires_at = clock_timestamp() + make_interval(secs => :lease)
     WHERE id = :id AND status = 'not_sent_retryable'
    RETURNING clock_timestamp() AS claimed_at
    """
)


def claim_next_scheduled(*, lease_seconds: int = LEASE_SECONDS, bind=None) -> Optional[DeliveryClaim]:
    """Claim the most overdue schedule's slot: insert its delivery row and re-arm
    the schedule, in one transaction. ``None`` when nothing is due.

    A slot that already has a row (a claim raced, or an older release ran it)
    is not claimed again: the schedule is re-armed and the next one is tried.
    """
    target = bind if bind is not None else engine
    for _ in range(25):
        token = secrets.token_hex(16)
        with target.begin() as conn:
            sched = conn.execute(_DUE_SCHEDULE).first()
            if sched is None:
                return None
            row = conn.execute(_INSERT_SCHEDULED, {
                "sid": sched.id, "uid": sched.user_id, "slot": sched.next_run,
                "key": _key_for_slot(sched.id, sched.next_run), "tok": token, "lease": lease_seconds,
            }).first()
            conn.execute(_REARM_SCHEDULE, {"sid": sched.id})
        if row is None:
            logger.warning("schedule %s: slot %s already has a delivery; not claimed again",
                           sched.id, sched.next_run)
            continue
        return DeliveryClaim(
            id=int(row.id), kind="scheduled", user_id=int(sched.user_id), schedule_id=int(sched.id),
            slot_at=sched.next_run, claim_token=token, attempts=int(row.attempts),
            claimed_at=row.created_at,
        )
    return None


def claim_next_retry(*, lease_seconds: int = LEASE_SECONDS, bind=None) -> Optional[DeliveryClaim]:
    """Claim the most overdue ``not_sent_retryable`` delivery, or ``None``."""
    target = bind if bind is not None else engine
    token = secrets.token_hex(16)
    with target.begin() as conn:
        row = conn.execute(_DUE_RETRY, {"max": MAX_ATTEMPTS, "window": WINDOW.total_seconds()}).first()
        if row is None:
            return None
        claimed_at = conn.execute(_RETRY_TO_PENDING, {
            "id": row.id, "tok": token, "lease": lease_seconds,
        }).scalar_one()
    return DeliveryClaim(
        id=int(row.id), kind="scheduled", user_id=int(row.user_id), schedule_id=int(row.schedule_id),
        slot_at=row.slot_at, claim_token=token, attempts=int(row.attempts), claimed_at=claimed_at,
    )


_COOLDOWN_PG = text(
    """
    UPDATE users SET last_manual_briefing_at = clock_timestamp()
     WHERE id = :uid AND is_active
       AND (last_manual_briefing_at IS NULL
            OR last_manual_briefing_at <= clock_timestamp() - make_interval(secs => :cd))
    RETURNING id, clock_timestamp() AS now
    """
)

_INSERT_MANUAL_PG = text(
    """
    INSERT INTO briefing_deliveries
           (kind, user_id, slot_at, idempotency_key, status, claim_token, lease_expires_at)
    VALUES ('manual', :uid, clock_timestamp(), :key, 'pending', :tok,
            clock_timestamp() + make_interval(secs => :lease))
    RETURNING id, created_at
    """
)


def start_manual(user_id: int, *, cooldown_seconds: int = MANUAL_COOLDOWN_SECONDS,
                 lease_seconds: int = LEASE_SECONDS, bind=None) -> Optional[DeliveryClaim]:
    """Start a manual send, or ``None`` while the user's cooldown runs.

    One transaction: the cooldown UPDATE (under the user's row lock, so
    concurrent requests serialize and re-check it) and the delivery INSERT
    commit together or not at all. The partial unique index on in-flight
    manual rows is the database's own backstop.
    """
    target = bind if bind is not None else engine
    token = secrets.token_hex(16)
    key = f"manual:{user_id}:{secrets.token_hex(16)}"
    try:
        with target.begin() as conn:
            if conn.dialect.name == "postgresql":
                ok = conn.execute(_COOLDOWN_PG, {"uid": user_id, "cd": cooldown_seconds}).first()
                if ok is None:
                    return None
                row = conn.execute(_INSERT_MANUAL_PG, {
                    "uid": user_id, "key": key, "tok": token, "lease": lease_seconds,
                }).one()
            else:  # the SQLite test harness: the same rule in Python time
                now = utcnow()
                ok = conn.execute(text(
                    "UPDATE users SET last_manual_briefing_at = :now WHERE id = :uid AND is_active "
                    "AND (last_manual_briefing_at IS NULL OR last_manual_briefing_at <= :cutoff) RETURNING id"
                ), {"uid": user_id, "now": now, "cutoff": now - timedelta(seconds=cooldown_seconds)}).first()
                if ok is None:
                    return None
                row = conn.execute(text(
                    "INSERT INTO briefing_deliveries (kind, user_id, slot_at, idempotency_key, status, "
                    "attempts, claim_token, lease_expires_at, row_version, created_at, updated_at) "
                    "VALUES ('manual', :uid, :now, :key, 'pending', 0, :tok, :lease_until, 1, :now, :now) "
                    "RETURNING id, created_at"
                ), {"uid": user_id, "now": now, "key": key, "tok": token,
                    "lease_until": now + timedelta(seconds=lease_seconds)}).one()
    except IntegrityError:
        logger.warning("user %s: a manual send is already in flight", user_id)
        return None
    created = row.created_at
    if isinstance(created, str):  # SQLite returns text
        created = datetime.fromisoformat(created)
    return DeliveryClaim(
        id=int(row.id), kind="manual", user_id=user_id, schedule_id=None, slot_at=created,
        claim_token=token, attempts=0, claimed_at=created,
    )


# ── the worker's guarded steps ───────────────────────────────────────────

_LOCK = text("SELECT id FROM briefing_deliveries WHERE id = :id FOR UPDATE")

# The schedule must still be on (scheduled rows) and the user active (all rows).
_STILL_WANTED = (
    "EXISTS (SELECT 1 FROM users u WHERE u.id = d.user_id AND u.is_active) "
    "AND (d.kind = 'manual' OR EXISTS (SELECT 1 FROM schedules s WHERE s.id = d.schedule_id AND s.enabled))"
)

_RENEW = text(
    f"""
    UPDATE briefing_deliveries d
       SET lease_expires_at = clock_timestamp() + make_interval(secs => :lease)
     WHERE d.id = :id AND d.claim_token = :tok AND d.status IN ('pending', 'sending')
       AND d.lease_expires_at > clock_timestamp() AND {_STILL_WANTED}
    RETURNING d.id
    """
)

_MARK_SENDING = text(
    f"""
    UPDATE briefing_deliveries d
       SET status = 'sending', attempts = d.attempts + 1,
           lease_expires_at = clock_timestamp() + make_interval(secs => :lease)
     WHERE d.id = :id AND d.claim_token = :tok AND d.status = 'pending'
       AND d.lease_expires_at > clock_timestamp() AND {_STILL_WANTED}
    RETURNING d.attempts
    """
)

# :retry asks for "retry if there is room, else final", decided here under the
# row lock: a scheduled row with attempts left whose next try still falls
# inside the window becomes not_sent_retryable.
_ROOM = (
    "(:retry AND d.kind = 'scheduled' AND d.attempts < :max "
    "AND clock_timestamp() + make_interval(secs => :backoff) < d.slot_at + make_interval(secs => :window))"
)

_FROM_SENDING = text(
    f"""
    UPDATE briefing_deliveries d
       SET status = CASE WHEN {_ROOM} THEN 'not_sent_retryable' ELSE :st END,
           next_attempt_at = CASE WHEN {_ROOM} THEN clock_timestamp() + make_interval(secs => :backoff) END,
           outcome_reason = :reason, http_status = :http,
           accepted_at = CASE WHEN :st = 'accepted' THEN clock_timestamp() END,
           claim_token = NULL, lease_expires_at = NULL
     WHERE d.id = :id AND d.claim_token = :tok AND d.status = 'sending'
       AND d.lease_expires_at > clock_timestamp()
    RETURNING d.status
    """
)

_LATE_ACCEPT = text(
    """
    UPDATE briefing_deliveries
       SET status = 'accepted', accepted_at = clock_timestamp(), http_status = :http, outcome_reason = NULL
     WHERE id = :id AND status = 'unknown' AND attempts = :attempt
    RETURNING id
    """
)

_FROM_PENDING = text(
    f"""
    UPDATE briefing_deliveries d
       SET status = CASE WHEN {_ROOM} THEN 'not_sent_retryable' ELSE :st END,
           next_attempt_at = CASE WHEN {_ROOM} THEN clock_timestamp() + make_interval(secs => :backoff) END,
           outcome_reason = :reason, claim_token = NULL, lease_expires_at = NULL
     WHERE d.id = :id AND d.claim_token = :tok AND d.status = 'pending'
       AND d.lease_expires_at > clock_timestamp()
    RETURNING d.status
    """
)


def _guarded(claim: DeliveryClaim, stmt, params: dict, bind) -> Optional[object]:
    target = bind if bind is not None else engine
    with target.begin() as conn:
        conn.execute(_LOCK, {"id": claim.id})
        return conn.execute(stmt, {"id": claim.id, "tok": claim.claim_token, **params}).first()


def renew(claim: DeliveryClaim, *, lease_seconds: int = LEASE_SECONDS, bind=None) -> bool:
    """Extend the lease. False if it lapsed, or the schedule or user was switched off."""
    return _guarded(claim, _RENEW, {"lease": lease_seconds}, bind) is not None


def mark_sending(claim: DeliveryClaim, *, lease_seconds: int = LEASE_SECONDS, bind=None) -> Optional[int]:
    """Commit ``pending -> sending`` before the request. Returns the attempt
    number, or ``None`` if the claim is lost or no longer wanted (send nothing)."""
    row = _guarded(claim, _MARK_SENDING, {"lease": lease_seconds}, bind)
    return None if row is None else int(row.attempts)


def record_outcome(claim: DeliveryClaim, attempt: int, outcome: str, reason: str,
                   http_status: Optional[int] = None, *, bind=None) -> Optional[str]:
    """Record a send's outcome (``accepted``, ``not_sent`` or ``unknown``).

    Returns the state written, or ``None`` if the lease was lost first (the
    reaper decides then; a late ``accepted`` is still recorded as evidence).
    """
    reason = normalize_reason(reason)
    backoff = BACKOFF[min(attempt, len(BACKOFF)) - 1]
    status = {"accepted": "accepted", "not_sent": "failed_final"}.get(outcome, "unknown")
    retry = outcome == "not_sent" and reason in RETRYABLE_REASONS
    row = _guarded(claim, _FROM_SENDING, {
        "st": status, "retry": retry, "reason": None if status == "accepted" else reason,
        "http": http_status, "backoff": backoff.total_seconds(),
        "max": MAX_ATTEMPTS, "window": WINDOW.total_seconds(),
    }, bind)
    if row is not None:
        return row.status
    if status == "accepted":
        target = bind if bind is not None else engine
        with target.begin() as conn:
            late = conn.execute(_LATE_ACCEPT, {"id": claim.id, "attempt": attempt, "http": http_status}).first()
        if late is not None:
            logger.warning("delivery %s: accepted after its lease expired; recorded late", claim.id)
            return "accepted"
    logger.warning("delivery %s: lease lost before recording %s", claim.id, status)
    return None


def finish_unsent(claim: DeliveryClaim, reason: str, *, bind=None) -> Optional[str]:
    """Close a ``pending`` row that will not be sent now.

    ``stale``: too late, ``skipped_stale``. ``generation``: the briefing could
    not be built; scheduled rows retry after :data:`GENERATION_BACKOFF` inside
    the window, manual ones fail. ``disabled``: ``failed_final``.
    """
    status = "skipped_stale" if reason == "stale" else "failed_final"
    row = _guarded(claim, _FROM_PENDING, {
        "st": status, "retry": reason == "generation", "reason": reason,
        "backoff": GENERATION_BACKOFF.total_seconds(), "max": MAX_ATTEMPTS, "window": WINDOW.total_seconds(),
    }, bind)
    return None if row is None else row.status


# ── the reaper ───────────────────────────────────────────────────────────

_REAP_SENDING = text(
    """
    UPDATE briefing_deliveries
       SET status = 'unknown', outcome_reason = 'lease_expired', claim_token = NULL, lease_expires_at = NULL
     WHERE status = 'sending' AND lease_expires_at <= clock_timestamp()
    RETURNING id, user_id, schedule_id, status
    """
)

_REAP_PENDING = text(
    """
    UPDATE briefing_deliveries
       SET status = CASE WHEN kind = 'scheduled' AND attempts < :max
                          AND clock_timestamp() < slot_at + make_interval(secs => :window)
                         THEN 'not_sent_retryable' ELSE 'failed_final' END,
           next_attempt_at = CASE WHEN kind = 'scheduled' AND attempts < :max
                                   AND clock_timestamp() < slot_at + make_interval(secs => :window)
                                  THEN clock_timestamp() END,
           outcome_reason = 'lease_expired', claim_token = NULL, lease_expires_at = NULL
     WHERE status = 'pending' AND lease_expires_at <= clock_timestamp()
    RETURNING id, user_id, schedule_id, status
    """
)

_REAP_RETRYABLE = text(
    """
    UPDATE briefing_deliveries d
       SET status = 'failed_final', next_attempt_at = NULL
     WHERE d.status = 'not_sent_retryable'
       AND (d.attempts >= :max
            OR clock_timestamp() >= d.slot_at + make_interval(secs => :window)
            OR NOT EXISTS (SELECT 1 FROM schedules s WHERE s.id = d.schedule_id AND s.enabled)
            OR NOT EXISTS (SELECT 1 FROM users u WHERE u.id = d.user_id AND u.is_active))
    RETURNING id, user_id, schedule_id, status
    """
)

_LOG = text(
    "INSERT INTO logs (user_id, schedule_id, status, message, created_at) "
    "VALUES (:uid, :sid, :st, :msg, clock_timestamp())"
)

_REAP_MESSAGES = {
    "unknown": "the send did not finish; outcome unknown, not resent",
    "not_sent_retryable": "the run stopped before sending; will retry",
    "failed_final": "not sent; no retry left",
}


def reap(*, bind=None) -> list[tuple[int, str]]:
    """Resolve expired leases and spent retries. Never sends anything.

    Returns ``(delivery id, new status)`` for every row changed, each also
    written to ``logs``.
    """
    params = {"max": MAX_ATTEMPTS, "window": WINDOW.total_seconds()}
    target = bind if bind is not None else engine
    changed = []
    with target.begin() as conn:
        for stmt in (_REAP_SENDING, _REAP_PENDING, _REAP_RETRYABLE):
            for row in conn.execute(stmt, params).all():
                changed.append((int(row.id), row.status))
                conn.execute(_LOG, {
                    "uid": row.user_id, "sid": row.schedule_id,
                    "st": {"unknown": "unknown", "not_sent_retryable": "retrying"}.get(row.status, "failed"),
                    "msg": f"delivery {row.id}: {_REAP_MESSAGES[row.status]}",
                })
    for delivery_id, status in changed:
        logger.warning("delivery %s reaped -> %s", delivery_id, status)
    return changed
