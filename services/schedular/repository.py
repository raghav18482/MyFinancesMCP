"""DB helpers for the scheduler: claims, leases, finishing and reclaiming runs.

Delivery semantics
------------------
At most one send *attempt* per schedule per slot. A slot can be missed (the
failure is recorded); it is never knowingly sent twice. Delivery itself is not
guaranteed: a message WhatsApp accepted can still fail to reach the recipient.

That guarantee comes from one rule, not from timing: **no code path ever starts
a second send for a slot**. A run that dies or stalls is reclaimed as
``unknown`` and the schedule moves to its *next* slot; the stuck slot is never
resent. The lease below only decides when a run is given up on.

Leases
------
Claiming a schedule sets ``status='running'``, a random ``claim_token`` and a
``lease_expires_at``. The worker renews the lease while it runs (a heartbeat),
and once more just before sending. Renewing and finishing both require the
token *and* a lease that has not expired, so a worker whose lease lapsed can
neither extend it nor record a result. A database lease cannot stop a stalled
worker from sending later, which is why the guarantee above does not depend
on it.

Time
----
Every time comparison uses ``clock_timestamp()``, the time when the statement
evaluates the row, not ``now()``, the start of the transaction. Renewing and
finishing first take the row lock (``_LOCK_SQL``) and only then run the guarded
UPDATE as a separate statement, so the lease is checked after any lock wait:
PostgreSQL re-checks a waiting UPDATE's WHERE clause only when the lock holder
changed the row, not when it merely locked it.

PostgreSQL only (``FOR UPDATE SKIP LOCKED``, ``clock_timestamp()``), except
:func:`claim_manual_send`, which also runs on SQLite for the test harness.
"""
from __future__ import annotations

import logging
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text

from db import engine
from db.models import utcnow

logger = logging.getLogger(__name__)

#: How long a claim holds without a heartbeat.
LEASE_SECONDS = 600
#: A run claimed this long after its slot is skipped, not sent.
STALE_AFTER = timedelta(hours=3)
#: Rows claimed by code that set no lease are reclaimed this long after their slot.
LEGACY_RECLAIM_AFTER_HOURS = 6
#: Manual "send now" may run at most once per this many seconds per user.
MANUAL_COOLDOWN_SECONDS = 600

_CLAIMABLE = "(status IS NULL OR status IN ('pending', 'done', 'failed', 'unknown'))"

# The next slot after the current time, anchored on the schedule's own slot
# time (not on when the run finished), so a briefing set for 08:30 stays at 08:30.
_ANCHORED_NEXT_RUN = (
    "next_run + GREATEST(1, FLOOR(EXTRACT(EPOCH FROM (clock_timestamp() - next_run)) "
    "/ (interval_minutes * 60))::int + 1) * make_interval(mins => interval_minutes)"
)

_CLAIM_SQL = text(
    f"""
    SELECT id, user_id, kind, interval_minutes, next_run
      FROM schedules
     WHERE enabled AND {_CLAIMABLE} AND next_run <= clock_timestamp()
     ORDER BY next_run
     LIMIT 1
       FOR UPDATE SKIP LOCKED
    """
)

_MARK_RUNNING_SQL = text(
    """
    UPDATE schedules
       SET status = 'running',
           claimed_at = clock_timestamp(),
           lease_expires_at = clock_timestamp() + make_interval(secs => :lease),
           claim_token = :tok
     WHERE id = :id
    RETURNING claimed_at
    """
)

_LOCK_SQL = text("SELECT id FROM schedules WHERE id = :id FOR UPDATE")

_RENEW_SQL = text(
    """
    UPDATE schedules s
       SET lease_expires_at = clock_timestamp() + make_interval(secs => :lease)
      FROM users u
     WHERE s.id = :id
       AND s.claim_token = :tok
       AND s.status = 'running'
       AND s.lease_expires_at > clock_timestamp()
       AND s.enabled
       AND u.id = s.user_id
       AND u.is_active
    RETURNING s.id
    """
)

_FINISH_SQL = text(
    f"""
    UPDATE schedules
       SET status = :st,
           last_run = clock_timestamp(),
           next_run = {_ANCHORED_NEXT_RUN},
           claim_token = NULL,
           lease_expires_at = NULL
     WHERE id = :id
       AND claim_token = :tok
       AND status = 'running'
       AND lease_expires_at > clock_timestamp()
    RETURNING next_run
    """
)

_RECLAIM_SQL = text(
    f"""
    UPDATE schedules
       SET status = 'unknown',
           next_run = {_ANCHORED_NEXT_RUN},
           claim_token = NULL,
           lease_expires_at = NULL
     WHERE status = 'running'
       AND (lease_expires_at <= clock_timestamp()
            OR (lease_expires_at IS NULL
                AND next_run < clock_timestamp() - make_interval(hours => :legacy_hours)))
    RETURNING id, user_id
    """
)

_RECLAIM_LOG_SQL = text(
    "INSERT INTO logs (user_id, schedule_id, status, message, created_at) "
    "VALUES (:uid, :sid, 'unknown', :msg, clock_timestamp())"
)


@dataclass(frozen=True)
class ClaimedSchedule:
    """A schedule this worker holds the lease on."""

    id: int
    user_id: int
    kind: str
    interval_minutes: int
    next_run: datetime  # the slot being run
    claim_token: str
    claimed_at: datetime

    @property
    def lateness(self) -> timedelta:
        return self.claimed_at - self.next_run


def claim_next_due(*, lease_seconds: int = LEASE_SECONDS, bind=None) -> Optional[ClaimedSchedule]:
    """Claim the most overdue schedule, or ``None`` if nothing is due.

    One row per call, so ``claimed_at`` is the real start time of each run.
    """
    target = bind if bind is not None else engine
    token = secrets.token_hex(16)
    with target.begin() as conn:
        row = conn.execute(_CLAIM_SQL).first()
        if row is None:
            return None
        claimed_at = conn.execute(
            _MARK_RUNNING_SQL, {"id": row.id, "lease": lease_seconds, "tok": token}
        ).scalar_one()
    return ClaimedSchedule(
        id=int(row.id),
        user_id=int(row.user_id),
        kind=str(row.kind),
        interval_minutes=int(row.interval_minutes),
        next_run=row.next_run,
        claim_token=token,
        claimed_at=claimed_at,
    )


def renew_lease(claim: ClaimedSchedule, *, lease_seconds: int = LEASE_SECONDS, bind=None) -> bool:
    """Extend the lease. False if it already expired, was taken over, or the
    schedule was disabled or its user deactivated meanwhile."""
    target = bind if bind is not None else engine
    with target.begin() as conn:
        conn.execute(_LOCK_SQL, {"id": claim.id})
        row = conn.execute(
            _RENEW_SQL, {"id": claim.id, "tok": claim.claim_token, "lease": lease_seconds}
        ).first()
    return row is not None


def finish_schedule(claim: ClaimedSchedule, status: str, *, bind=None) -> bool:
    """Record the run's outcome and arm the next slot. False if the claim was lost.

    ``status``: ``done`` (accepted or deliberately skipped), ``failed`` (known
    not sent) or ``unknown`` (sent or not, nobody knows).
    """
    if status not in ("done", "failed", "unknown"):
        raise ValueError(f"not a finishing status: {status!r}")
    target = bind if bind is not None else engine
    with target.begin() as conn:
        conn.execute(_LOCK_SQL, {"id": claim.id})
        row = conn.execute(
            _FINISH_SQL, {"id": claim.id, "tok": claim.claim_token, "st": status}
        ).first()
    if row is None:
        logger.warning("schedule %s: claim lost before finishing (%s not recorded)", claim.id, status)
        return False
    logger.info("schedule %s -> %s, next_run=%s", claim.id, status, row.next_run)
    return True


def reclaim_expired(*, bind=None) -> list[tuple[int, int]]:
    """Give up on runs whose lease lapsed: mark them ``unknown``, never resend.

    Each moves to its next slot and gets a log row. Also covers rows claimed by
    older code that set no lease, once they are well past their slot.
    """
    target = bind if bind is not None else engine
    with target.begin() as conn:
        rows = conn.execute(_RECLAIM_SQL, {"legacy_hours": LEGACY_RECLAIM_AFTER_HOURS}).all()
        for row in rows:
            conn.execute(_RECLAIM_LOG_SQL, {
                "uid": row.user_id, "sid": row.id,
                "msg": "reclaimed: the run did not finish; outcome unknown, not resent",
            })
    for row in rows:
        logger.warning("schedule %s reclaimed as unknown (user_id=%s)", row.id, row.user_id)
    return [(int(r.id), int(r.user_id)) for r in rows]


def next_slot(scheduled: datetime, interval_minutes: int, now: datetime) -> datetime:
    """Python twin of the SQL anchoring: the first ``scheduled + k*interval`` after ``now``."""
    step = timedelta(minutes=interval_minutes)
    k = max(1, math.floor((now - scheduled) / step) + 1)
    return scheduled + k * step


def claim_manual_send(db_session, user_id: int, *, cooldown_seconds: int = MANUAL_COOLDOWN_SECONDS) -> bool:
    """Atomically start a manual send for ``user_id``, at most once per cooldown.

    One UPDATE, under the user's row lock: concurrent requests are serialized,
    and each re-checks the cooldown after the lock (PostgreSQL re-evaluates the
    WHERE clause, and ``clock_timestamp()`` with it). Durable across restarts.
    Commits on success.
    """
    conn = db_session.connection()
    if conn.dialect.name == "postgresql":
        row = conn.execute(text(
            """
            UPDATE users SET last_manual_briefing_at = clock_timestamp()
             WHERE id = :uid AND is_active
               AND (last_manual_briefing_at IS NULL
                    OR last_manual_briefing_at <= clock_timestamp() - make_interval(secs => :cd))
            RETURNING id
            """
        ), {"uid": user_id, "cd": cooldown_seconds}).first()
    else:  # the SQLite test harness: same rule, Python time
        now = utcnow()
        row = conn.execute(text(
            "UPDATE users SET last_manual_briefing_at = :now "
            "WHERE id = :uid AND is_active "
            "AND (last_manual_briefing_at IS NULL OR last_manual_briefing_at <= :cutoff) "
            "RETURNING id"
        ), {"uid": user_id, "now": now, "cutoff": now - timedelta(seconds=cooldown_seconds)}).first()
    db_session.commit()
    return row is not None
