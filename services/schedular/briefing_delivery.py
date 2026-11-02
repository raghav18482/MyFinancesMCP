"""The one way to send a briefing over WhatsApp.

A briefing may only go out under a claim:

- a :class:`~services.schedular.ledger.DeliveryClaim`, a leased ``pending``
  row in the delivery ledger: a scheduled slot, a retry of one, or a manual
  "send now" that passed the atomic cooldown (:func:`start_manual_send`). The
  row is committed as ``sending`` *before* the request (which also checks the
  schedule is still on and the user still active); if that fails, nothing is
  sent. The outcome is recorded on the row afterwards.
- a :class:`CliClaim`, for the local development CLI, which is refused in
  production and writes no ledger row.

:func:`send_briefing` returns a :class:`DeliveryResult` with one of
``accepted``, ``not_sent``, ``unknown`` or ``aborted`` (claim lost before
sending, nothing sent). It never raises for delivery failures and never
retries an ``unknown``; retries of certain not-sent attempts are separate
claims (see :mod:`services.schedular.ledger`).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional, Union

from services.schedular import ledger, whatsapp
from services.schedular.ledger import DeliveryClaim

logger = logging.getLogger(__name__)

ABORTED = "aborted"


@dataclass(frozen=True)
class CliClaim:
    """The local development CLI's claim. Build it with :func:`cli_claim`."""

    source: str = "cli"


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str  # accepted | not_sent | unknown | aborted
    reason: str
    #: The ledger state written (e.g. ``not_sent_retryable``), or None if none was.
    recorded: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.outcome == whatsapp.ACCEPTED


Claim = Union[DeliveryClaim, CliClaim]


def start_manual_send(user_id: int, *, bind=None) -> Optional[DeliveryClaim]:
    """A manual-send claim, or ``None`` while the user's cooldown runs."""
    return ledger.start_manual(user_id, bind=bind)


def _is_production() -> bool:
    # Same rule as web.security_config.is_production; services may not import web.
    if os.environ.get("APP_ENV", "").strip().lower() in ("production", "prod"):
        return True
    return os.environ.get("RENDER", "").strip().lower() == "true"


def cli_claim() -> CliClaim:
    """For the local development CLI only. Refused in production."""
    if _is_production():
        raise RuntimeError("the briefing CLI cannot send WhatsApp messages in production")
    return CliClaim()


async def send_briefing(claim: Claim, phone: Optional[str], message: str) -> DeliveryResult:
    if isinstance(claim, CliClaim):
        try:
            await whatsapp._post(phone, message)
        except whatsapp.DeliveryError as e:
            return DeliveryResult(e.outcome, e.reason)
        return DeliveryResult(whatsapp.ACCEPTED, "accepted")
    if not isinstance(claim, DeliveryClaim):
        raise TypeError("send_briefing needs a DeliveryClaim or CliClaim")

    # The last check before the irreversible step, committed before it: from
    # here on the reaper treats this row as possibly sent.
    attempt = await asyncio.to_thread(ledger.mark_sending, claim)
    if attempt is None:
        logger.warning("delivery %s: claim lost or no longer wanted; not sending", claim.id)
        return DeliveryResult(ABORTED, "lease_lost_or_disabled")

    http_status: Optional[int] = None
    try:
        http_status = await whatsapp._post(phone, message)
    except whatsapp.DeliveryError as e:
        outcome, reason, http_status = e.outcome, e.reason, e.http_status
    else:
        outcome, reason = whatsapp.ACCEPTED, "accepted"
    recorded = await asyncio.to_thread(ledger.record_outcome, claim, attempt, outcome, reason, http_status)
    return DeliveryResult(outcome, reason, recorded)
