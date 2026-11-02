"""Per-delivery pipeline: decrypt -> session -> briefing -> WhatsApp -> log.

Each call to :func:`run_one` handles one ``pending`` delivery this worker has
claimed (:mod:`services.schedular.ledger`): a scheduled slot, a retry of one,
or a manual "send now". It:

1. Skips a scheduled slot, sending nothing, if it is more than three hours
   past (``ledger.WINDOW``): a morning briefing at night helps nobody.
2. Keeps the delivery's lease alive with a heartbeat while it works.
3. Loads the user, logs in to Angel One (in a worker thread) and builds the
   briefing. If that fails, nothing was sent: a scheduled slot is retried
   later inside its window, a manual one is not.
4. Sends it through :func:`services.schedular.briefing_delivery.send_briefing`,
   which commits the row as ``sending`` first and records the outcome after.
5. Writes a row in ``logs`` for the dashboard.
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
from services.schedular import ledger
from services.schedular.briefing_delivery import ABORTED, DeliveryResult, send_briefing
from services.schedular.daily_briefing import generate_daily_briefing
from services.schedular.ledger import DeliveryClaim
from session_manager import sessions

logger = logging.getLogger(__name__)

_MAX_LOG_MESSAGE_CHARS = 500
#: How often a running job renews its lease (well under ledger.LEASE_SECONDS).
HEARTBEAT_SECONDS = 60

# ledger state -> log status shown on the dashboard
_LOG_STATUS = {
    "accepted": "success",
    "not_sent_retryable": "retrying",
    "failed_final": "failed",
    "unknown": "unknown",
    "skipped_stale": "skipped_stale",
}


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _write_log(claim: DeliveryClaim, *, status: str, message: str, duration_ms: int) -> None:
    """Persist a row in ``logs``. Never raises — logging must not break a run."""
    try:
        with get_session() as db:
            db.add(
                Log(
                    user_id=claim.user_id,
                    schedule_id=claim.schedule_id,
                    status=status,
                    message=message[:_MAX_LOG_MESSAGE_CHARS],
                    duration_ms=duration_ms,
                )
            )
            db.commit()
    except Exception:
        logger.exception("failed to persist log for delivery %s", claim.id)


async def _finish_unsent(claim: DeliveryClaim, reason: str) -> str | None:
    try:
        return await asyncio.to_thread(ledger.finish_unsent, claim, reason)
    except Exception:
        # The lease will lapse and the reaper will resolve the row.
        logger.exception("delivery %s: could not record %s", claim.id, reason)
        return None


async def _heartbeat(claim: DeliveryClaim, lost: asyncio.Event) -> None:
    """Renew the lease until cancelled; set ``lost`` the moment a renewal fails."""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            ok = await asyncio.to_thread(ledger.renew, claim)
        except Exception:
            # A database hiccup: try again next beat. If the lease does lapse,
            # marking the row as sending fails and nothing is sent.
            logger.warning("delivery %s: lease renewal errored; will retry", claim.id)
            continue
        if not ok:
            lost.set()
            return


def _label(claim: DeliveryClaim) -> str:
    return "manual send" if claim.kind == "manual" else f"slot {claim.slot_at:%Y-%m-%d %H:%M}"


async def run_one(claim: DeliveryClaim) -> None:
    """Execute one claimed delivery end-to-end. Catches all exceptions."""
    started = time.perf_counter()

    if claim.kind == "scheduled" and claim.lateness > ledger.WINDOW:
        await _finish_unsent(claim, "stale")
        _write_log(claim, status="skipped_stale", duration_ms=0,
                   message=f"skipped: started {int(claim.lateness.total_seconds() // 60)} min after its slot")
        return

    sid = f"deliv-{claim.id}-{uuid4().hex[:8]}"
    session_created = False
    lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(claim, lost))
    result: DeliveryResult | None = None
    message = ""

    try:
        with get_session() as db:
            user = db.get(User, claim.user_id)
            if user is None or not user.is_active:
                user = None
            else:
                api_key = user.angel_api_key
                client_id = user.angel_client_id
                phone = user.whatsapp_number
                password = decrypt_value(user.angel_password_encrypted)
                totp_secret = decrypt_value(user.angel_totp_secret_encrypted)
        if user is None:
            await _finish_unsent(claim, "disabled")
            _write_log(claim, status="skipped", message=f"{_label(claim)}: not sent, user inactive",
                       duration_ms=_ms_since(started))
            return

        # The Angel One login blocks for seconds; the scheduler shares the
        # web server's event loop, so it runs in a worker thread.
        await anyio.to_thread.run_sync(
            partial(sessions.create_session, sid, api_key, client_id, password, totp_secret)
        )
        session_created = True
        message = await generate_daily_briefing(sid)
        if lost.is_set():
            result = DeliveryResult(ABORTED, "lease_lost")
        else:
            result = await send_briefing(claim, phone, message)

    except Exception as exc:
        # Before any send: login, briefing or data failed, so nothing went out.
        logger.exception("delivery %s failed before sending (user_id=%s)", claim.id, claim.user_id)
        state = await _finish_unsent(claim, "generation")
        _write_log(claim, status=_LOG_STATUS.get(state or "", "failed"),
                   message=f"{_label(claim)}: not sent: {exc}", duration_ms=_ms_since(started))
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
        # Nothing was sent. If the claim is still ours (schedule switched off,
        # user deactivated) the row is closed now; otherwise the reaper does it.
        await _finish_unsent(claim, "disabled")
        _write_log(claim, status="skipped", message=f"{_label(claim)}: not sent: {result.reason}",
                   duration_ms=duration)
        return

    status = _LOG_STATUS.get(result.recorded or "", {"accepted": "success"}.get(result.outcome, "unknown"))
    _write_log(
        claim,
        status=status,
        message=f"{_label(claim)}: {result.outcome} ({result.reason}), {len(message)} chars",
        duration_ms=duration,
    )
    logger.info("delivery %s %s -> %s in %dms (user_id=%s)", claim.id, result.outcome,
                result.recorded, duration, claim.user_id)
