"""Schedule-level helpers left after the delivery ledger (R10) took over claims.

Claiming, sending and retrying now go through :mod:`services.schedular.ledger`.
What remains here:

- :func:`reclaim_expired`: schedules the R5 scheduler marked ``running`` and
  never finished (a crash, or the deploy that replaced it). They are marked
  ``unknown`` and moved to their next slot; the stuck slot is never resent.
  R10 itself never sets ``running``.
- :func:`next_slot`: the Python twin of the slot anchoring the claim SQL uses.

PostgreSQL only (``clock_timestamp()``).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import text

from db import engine

logger = logging.getLogger(__name__)

#: Rows claimed by code that set no lease are reclaimed this long after their slot.
LEGACY_RECLAIM_AFTER_HOURS = 6

# The next slot after the current time, anchored on the schedule's own slot
# time (not on when the run finished), so a briefing set for 08:30 stays at 08:30.
_ANCHORED_NEXT_RUN = (
    "next_run + GREATEST(1, FLOOR(EXTRACT(EPOCH FROM (clock_timestamp() - next_run)) "
    "/ (interval_minutes * 60))::int + 1) * make_interval(mins => interval_minutes)"
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


def reclaim_expired(*, bind=None) -> list[tuple[int, int]]:
    """Give up on schedule runs left ``running`` by older code: ``unknown``, never resent.

    Each moves to its next slot and gets a log row. Covers rows whose R5 lease
    lapsed, and rows from before R5 (no lease) once well past their slot.
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
