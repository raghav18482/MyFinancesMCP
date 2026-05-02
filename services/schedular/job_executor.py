"""Per-schedule pipeline: decrypt -> session -> briefing -> WhatsApp -> log.

Each call to :func:`run_one` corresponds to one row already claimed by
:func:`services.schedular.repository.claim_due_schedules`, i.e. a row whose
``status`` is already ``'running'``. The executor is therefore *not*
responsible for concurrency control; its only job is to:

1. Load the user from the DB and decrypt their Angel credentials.
2. Create a short-lived Angel session under an ephemeral ``session_id``.
3. Run :func:`generate_daily_briefing` and send a WhatsApp message.
4. Insert a row in ``logs`` and call ``finish_schedule(...)`` to arm the
   next run.
5. Always tear down the in-memory session in ``finally`` so credentials
   never outlive the job.

A failure here never escapes — exceptions are caught, logged, recorded in
``logs``, and the schedule is marked ``failed`` (not abandoned).
"""
from __future__ import annotations

import logging
import time
from uuid import uuid4

from db import decrypt_value, get_session
from db.models import Log, User
from services.schedular import repository, whatsapp
from services.schedular.daily_briefing import generate_daily_briefing
from services.schedular.repository import ClaimedSchedule
from session_manager import sessions

logger = logging.getLogger(__name__)

_MAX_LOG_MESSAGE_CHARS = 500


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


async def run_one(claim: ClaimedSchedule) -> None:
    """Execute one claimed schedule end-to-end. Catches all exceptions."""
    started = time.perf_counter()
    sid = f"sched-{claim.id}-{uuid4().hex[:8]}"
    session_created = False

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

        sessions.create_session(sid, api_key, client_id, password, totp_secret)
        session_created = True

        message = await generate_daily_briefing(sid)
        await whatsapp.send(phone, message)

        duration = _ms_since(started)
        _write_log(
            claim,
            status="success",
            message=f"sent {len(message)} chars to {phone or '<unset>'}",
            duration_ms=duration,
        )
        repository.finish_schedule(
            claim.id, success=True, interval_minutes=claim.interval_minutes
        )
        logger.info(
            "schedule %s ok in %dms (user_id=%s)",
            claim.id,
            duration,
            claim.user_id,
        )

    except Exception as exc:
        duration = _ms_since(started)
        logger.exception(
            "schedule %s failed after %dms (user_id=%s)",
            claim.id,
            duration,
            claim.user_id,
        )
        _write_log(
            claim,
            status="failed",
            message=str(exc),
            duration_ms=duration,
        )
        repository.finish_schedule(
            claim.id, success=False, interval_minutes=claim.interval_minutes
        )

    finally:
        if session_created:
            try:
                sessions.remove_session(sid)
            except Exception:
                logger.exception("failed to remove ephemeral session %s", sid)
