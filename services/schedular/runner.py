"""Single APScheduler instance — the heart of Phase 2.

A process-wide :class:`AsyncIOScheduler` ticks once a minute and runs
:func:`check_and_run_jobs`, which reaps expired deliveries, then claims due
retries and schedule slots one at a time through the delivery ledger
(:mod:`services.schedular.ledger`) and runs each with
:mod:`services.schedular.job_executor`.

Overlap protection:
- ``max_instances=1`` on the APScheduler job (per process).
- ``FOR UPDATE SKIP LOCKED`` in the claim SQL, and one ledger row per
  schedule slot (unique key), across processes.

Constraints enforced here:
- Only one scheduler per process: :func:`start` is a no-op if already running.
- Only one process should set ``SCHEDULER_ENABLED=1``; ``main.py`` gates the
  call to :func:`start` on that env var.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from services.schedular import job_executor, ledger, repository

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
    import asyncio

    try:
        await asyncio.to_thread(ledger.reap)
        # Schedules left 'running' by the R5 scheduler during the deploy overlap.
        await asyncio.to_thread(repository.reclaim_expired)
    except Exception:
        logger.exception("scheduler tick: reap failed")

    # One claim at a time, so each lease starts when its run does. Retries of
    # certain not-sent attempts go first: their window is shorter.
    for _ in range(_BATCH_SIZE):
        try:
            claim = await asyncio.to_thread(ledger.claim_next_retry)
            if claim is None:
                claim = await asyncio.to_thread(ledger.claim_next_scheduled)
        except Exception:
            logger.exception("scheduler tick: claim failed")
            return
        if claim is None:
            return
        try:
            await job_executor.run_one(claim)
        except Exception:
            logger.exception(
                "scheduler tick: unexpected error escaping run_one for delivery %s", claim.id,
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
