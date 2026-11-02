"""The one way to send a briefing over WhatsApp.

A briefing may only go out under a claim:

- :class:`ScheduleClaim`, a scheduler lease on one slot of one schedule. The
  lease is renewed immediately before sending (which also checks the schedule
  is still enabled and its user still active); if that fails, nothing is sent.
- :class:`ManualClaim`, a manual "send now" that passed the atomic cooldown
  (:func:`start_manual_send`), or the local development CLI, which is refused
  in production.

Whatever happens, :func:`send_briefing` returns a :class:`DeliveryResult` with
one of ``accepted``, ``not_sent``, ``unknown`` or ``aborted`` (lease lost before
sending, nothing sent). It never raises for delivery failures, and it never
retries an ``unknown``: see :mod:`services.schedular.whatsapp`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Union

from services.schedular import repository, whatsapp
from services.schedular.repository import ClaimedSchedule

logger = logging.getLogger(__name__)

ABORTED = "aborted"


@dataclass(frozen=True)
class ScheduleClaim:
    schedule: ClaimedSchedule


@dataclass(frozen=True)
class ManualClaim:
    """Proof that a manual send passed its cooldown. Build it with
    :func:`start_manual_send` or :func:`cli_claim`, never directly."""

    user_id: Optional[int]
    source: str  # "web" | "cli"


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str  # accepted | not_sent | unknown | aborted
    reason: str

    @property
    def accepted(self) -> bool:
        return self.outcome == whatsapp.ACCEPTED


Claim = Union[ScheduleClaim, ManualClaim]


def start_manual_send(db_session, user_id: int) -> Optional[ManualClaim]:
    """A manual-send claim, or ``None`` while the user's cooldown runs."""
    if repository.claim_manual_send(db_session, user_id):
        return ManualClaim(user_id=user_id, source="web")
    return None


def _is_production() -> bool:
    # Same rule as web.security_config.is_production; services may not import web.
    if os.environ.get("APP_ENV", "").strip().lower() in ("production", "prod"):
        return True
    return os.environ.get("RENDER", "").strip().lower() == "true"


def cli_claim() -> ManualClaim:
    """For the local development CLI only. Refused in production."""
    if _is_production():
        raise RuntimeError("the briefing CLI cannot send WhatsApp messages in production")
    return ManualClaim(user_id=None, source="cli")


async def send_briefing(claim: Claim, phone: Optional[str], message: str) -> DeliveryResult:
    if isinstance(claim, ScheduleClaim):
        # The last check before the irreversible step. A worker that stalls
        # after this can still send late, but the reclaimed slot is never sent
        # by anyone else (see repository), so at most one attempt is made.
        if not repository.renew_lease(claim.schedule):
            logger.warning("schedule %s: lease lost or schedule disabled; not sending",
                           claim.schedule.id)
            return DeliveryResult(ABORTED, "lease_lost_or_disabled")
    elif not isinstance(claim, ManualClaim):
        raise TypeError("send_briefing needs a ScheduleClaim or ManualClaim")

    try:
        await whatsapp._post(phone, message)
    except whatsapp.DeliveryError as e:
        return DeliveryResult(e.outcome, e.reason)
    return DeliveryResult(whatsapp.ACCEPTED, "accepted")
