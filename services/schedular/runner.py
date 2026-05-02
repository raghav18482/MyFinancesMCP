"""Single APScheduler instance — the heart of Phase 2.

A process-wide :class:`AsyncIOScheduler` ticks once a minute and runs
:func:`check_and_run_jobs`, which claims due schedules under a row-level
lock and dispatches them to :mod:`services.schedular.job_executor`.

Three layers of overlap protection:
- ``max_instances=1`` on the APScheduler job (per process).
- ``status='running'`` set during the claim transaction (per row).
- ``FOR UPDATE SKIP LOCKED`` in the claim SQL (per cluster).

Constraints enforced here:
- Only one scheduler per process: :func:`start` is a no-op if already running.
- Only one process should set ``SCHEDULER_ENABLED=1``; ``main.py`` gates the
  call to :func:`start` on that env var.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from services.schedular import job_executor, repository

logger = logging.getLogger(__name__)

_JOB_ID = "check_and_run_jobs"
_BATCH_SIZE = 25
_TICK_MINUTES = 1
_MISFIRE_GRACE_SEC = 30

_scheduler: AsyncIOScheduler | None = None


async def check_and_run_jobs() -> None:
    """One scheduler tick: claim due schedules and run each one sequentially.

    Sequential execution is intentional — running multiple briefings in
    parallel would compound Angel's per-account historical-candle rate
    limits. Cross-user concurrency can be added later by switching to
    ``asyncio.gather`` with a semaphore.
    """
    try:
        claims = repository.claim_due_schedules(batch=_BATCH_SIZE)
    except Exception:
        logger.exception("scheduler tick: claim failed")
        return

    if not claims:
        return

    logger.info("scheduler tick: claimed %d schedule(s)", len(claims))
    for claim in claims:
        try:
            await job_executor.run_one(claim)
        except Exception:
            logger.exception(
                "scheduler tick: unexpected error escaping run_one for schedule %s",
                claim.id,
            )


def start() -> None:
    """Start the singleton scheduler. No-op if already started."""
    global _scheduler
    if _scheduler is not None:
        logger.info("scheduler already running, skipping start()")
        return

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_and_run_jobs,
        trigger=IntervalTrigger(minutes=_TICK_MINUTES),
        id=_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_MISFIRE_GRACE_SEC,
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "scheduler started: every %d min, batch=%d, max_instances=1",
        _TICK_MINUTES,
        _BATCH_SIZE,
    )


def stop() -> None:
    """Stop the singleton scheduler if it is running."""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("scheduler shutdown raised; ignoring")
    finally:
        _scheduler = None
        logger.info("scheduler stopped")
