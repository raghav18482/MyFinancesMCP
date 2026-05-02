"""DB helpers for the scheduler — the only place that uses ``FOR UPDATE SKIP LOCKED``.

Two short transactions:

- ``claim_due_schedules`` atomically picks rows that are due and flips their
  ``status`` to ``running``. Postgres' ``SKIP LOCKED`` ensures no two workers
  ever see the same row, so the rest of the pipeline can run lock-free.
- ``finish_schedule`` records ``last_run`` / ``next_run`` and resets ``status``.

The module talks raw SQL (via the shared SQLModel ``engine``) on purpose:
SQLModel's ORM does not expose ``FOR UPDATE SKIP LOCKED`` cleanly, and keeping
the lock window to a single short statement is the whole point.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text

from db import engine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedSchedule:
    """A schedule row already flipped to ``status='running'``."""

    id: int
    user_id: int
    kind: str
    interval_minutes: int
    next_run: datetime


_CLAIM_SQL = text(
    """
    SELECT id, user_id, kind, interval_minutes, next_run
      FROM schedules
     WHERE enabled = true
       AND status IN ('pending', 'done', 'failed')
       AND next_run <= now()
     ORDER BY next_run
     FOR UPDATE SKIP LOCKED
     LIMIT :batch
    """
)

_MARK_RUNNING_SQL = text(
    "UPDATE schedules SET status = 'running' WHERE id = ANY(:ids)"
)

_FINISH_SQL = text(
    """
    UPDATE schedules
       SET status = :st,
           last_run = now(),
           next_run = :nr
     WHERE id = :id
    """
)


def claim_due_schedules(batch: int = 25) -> list[ClaimedSchedule]:
    """Atomically claim up to ``batch`` due schedules under a row-level lock.

    Returns the rows that this caller now owns (their ``status`` has been
    flipped to ``running`` in the same transaction). An empty list means
    nothing is due, or another worker already has the only due rows locked.
    """
    with engine.begin() as conn:
        rows = conn.execute(_CLAIM_SQL, {"batch": batch}).all()
        if not rows:
            return []
        ids = [int(r.id) for r in rows]
        conn.execute(_MARK_RUNNING_SQL, {"ids": ids})
        return [
            ClaimedSchedule(
                id=int(r.id),
                user_id=int(r.user_id),
                kind=str(r.kind),
                interval_minutes=int(r.interval_minutes),
                next_run=r.next_run,
            )
            for r in rows
        ]


def finish_schedule(
    schedule_id: int,
    *,
    success: bool,
    interval_minutes: int,
) -> None:
    """Record the result of a run and arm the schedule for its next tick."""
    new_status = "done" if success else "failed"
    next_run = datetime.utcnow() + timedelta(minutes=interval_minutes)
    with engine.begin() as conn:
        conn.execute(
            _FINISH_SQL,
            {"st": new_status, "nr": next_run, "id": schedule_id},
        )
    logger.info(
        "schedule %s -> %s, next_run=%s",
        schedule_id,
        new_status,
        next_run.isoformat(timespec="seconds"),
    )
