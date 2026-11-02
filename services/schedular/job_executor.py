"""Per-schedule pipeline: decrypt -> session -> briefing -> WhatsApp -> log.

Each call to :func:`run_one` handles one schedule this worker has claimed with
a lease (:func:`services.schedular.repository.claim_next_due`). It:

1. Skips the run, sending nothing, if it started more than three hours after
   its slot (``repository.STALE_AFTER``): a morning briefing at night helps
   nobody.
2. Keeps the lease alive with a heartbeat while it works.
3. Loads the user, logs in to Angel One (in a worker thread) and builds the
   briefing.
4. Sends it through :func:`services.schedular.briefing_delivery.send_briefing`,
   which renews the lease once more first and refuses to send without it.
5. Records the outcome in ``logs`` and finishes the schedule: ``done`` when
   WhatsApp accepted it (or the run was skipped), ``failed`` when it was
   certainly not sent, ``unknown`` when nobody can tell. Nothing is ever
   resent; the schedule moves to its next slot.
6. Always tears down the in-memory broker session.

A failure here never escapes: it is caught, logged and recorded.
"""
from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from uuid import uuid4

import anyio

from db import decrypt_value, get_session
from db.models import Log, User
from services.schedular import repository, whatsapp
from services.schedular.briefing_delivery import ABORTED, ScheduleClaim, send_briefing
from services.schedular.daily_briefing import generate_daily_briefing
from services.schedular.repository import ClaimedSchedule
from session_manager import sessions

logger = logging.getLogger(__name__)

_MAX_LOG_MESSAGE_CHARS = 500
#: How often a running job renews its lease (well under repository.LEASE_SECONDS).
HEARTBEAT_SECONDS = 60

# delivery outcome -> (schedule status, log status)
_OUTCOMES = {
    whatsapp.ACCEPTED: ("done", "success"),
    whatsapp.NOT_SENT: ("failed", "failed"),
    whatsapp.UNKNOWN: ("unknown", "unknown"),
}


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _write_log(
    claim: ClaimedSchedule,
    *,
    status: str,
    message: str,
    duration_ms: int,
) -> None:
    """Persist a row in ``logs``. Never raises — logging must not break a run."""
    try:
        with get_session() as db:
            db.add(
                Log(
                    user_id=claim.user_id,
                    schedule_id=claim.id,
                    status=status,
                    message=message[:_MAX_LOG_MESSAGE_CHARS],
                    duration_ms=duration_ms,
                )
            )
            db.commit()
    except Exception:
        logger.exception("failed to persist log for schedule %s", claim.id)


async def _finish(claim: ClaimedSchedule, status: str) -> None:
    try:
        await asyncio.to_thread(repository.finish_schedule, claim, status)
    except Exception:
        # The lease will lapse and the reaper will move the schedule on.
        logger.exception("schedule %s: could not record %s", claim.id, status)


async def _heartbeat(claim: ClaimedSchedule, lost: asyncio.Event) -> None:
    """Renew the lease until cancelled; set ``lost`` the moment a renewal fails."""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            ok = await asyncio.to_thread(repository.renew_lease, claim)
        except Exception:
            # A database hiccup: try again next beat. If the lease does lapse,
            # the final renewal before sending fails and nothing is sent.
            logger.warning("schedule %s: lease renewal errored; will retry", claim.id)
            continue
        if not ok:
            lost.set()
            return


async def run_one(claim: ClaimedSchedule) -> None:
    """Execute one claimed schedule end-to-end. Catches all exceptions."""
    started = time.perf_counter()

    if claim.lateness > repository.STALE_AFTER:
        _write_log(
            claim,
            status="skipped_stale",
            message=f"skipped: started {int(claim.lateness.total_seconds() // 60)} min after its slot",
            duration_ms=0,
        )
        await _finish(claim, "done")
        return

    sid = f"sched-{claim.id}-{uuid4().hex[:8]}"
    session_created = False
    lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(claim, lost))
    result = None

    try:
        with get_session() as db:
            user = db.get(User, claim.user_id)
            if user is None:
                raise RuntimeError(f"user {claim.user_id} not found")
            if not user.is_active:
                raise RuntimeError(f"user {claim.user_id} is inactive")

            api_key = user.angel_api_key
            client_id = user.angel_client_id
            phone = user.whatsapp_number
            password = decrypt_value(user.angel_password_encrypted)
            totp_secret = decrypt_value(user.angel_totp_secret_encrypted)

        # The Angel One login blocks for seconds; the scheduler shares the
        # web server's event loop, so it runs in a worker thread.
        await anyio.to_thread.run_sync(
            partial(sessions.create_session, sid, api_key, client_id, password, totp_secret)
        )
        session_created = True

        message = await generate_daily_briefing(sid)
        if lost.is_set():
            from services.schedular.briefing_delivery import DeliveryResult

            result = DeliveryResult(ABORTED, "lease_lost")
        else:
            result = await send_briefing(ScheduleClaim(claim), phone, message)

    except Exception as exc:
        # Before any send: login, briefing or data failed, so nothing went out.
        logger.exception("schedule %s failed before sending (user_id=%s)", claim.id, claim.user_id)
        _write_log(claim, status="failed", message=f"not sent: {exc}", duration_ms=_ms_since(started))
        await _finish(claim, "failed")
        return

    finally:
        heartbeat.cancel()
        if session_created:
            try:
                sessions.remove_session(sid)
            except Exception:
                logger.exception("failed to remove ephemeral session %s", sid)

    duration = _ms_since(started)
    if result.outcome == ABORTED:
        # Nothing was sent; the log says so. If the lease is still ours (the
        # schedule was disabled or the user deactivated) the schedule moves on
        # now; if it was lost, finishing fails and the reaper moves it on.
        _write_log(claim, status="skipped", message=f"not sent: {result.reason}", duration_ms=duration)
        await _finish(claim, "done")
        return

    status, log_status = _OUTCOMES[result.outcome]
    _write_log(
        claim,
        status=log_status,
        message=f"{result.outcome} ({result.reason}), {len(message)} chars",
        duration_ms=duration,
    )
    await _finish(claim, status)
    logger.info("schedule %s %s in %dms (user_id=%s)", claim.id, result.outcome, duration, claim.user_id)
