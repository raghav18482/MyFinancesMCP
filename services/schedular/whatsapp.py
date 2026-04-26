"""WhatsApp delivery — stub implementation for Phase 2.

The scheduler depends only on the ``send(phone, message)`` coroutine. When a
real provider (Twilio, Meta Cloud API, etc.) is chosen, swap the body of
``send`` and keep the signature; nothing else in the scheduler needs to change.

The DB ``logs`` table already records every send attempt's status, message
preview, and duration, so an audit trail exists from day one.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PREVIEW_CHARS = 140


async def send(phone: str | None, message: str) -> None:
    """Phase-2 stub sender: log the outbound message and return.

    A non-empty ``phone`` is preferred but not required — we still log the
    payload so missing-number cases surface in operations.
    """
    target = (phone or "").strip() or "<unset>"
    preview = message.replace("\n", " \u23ce ")[:_PREVIEW_CHARS]
    suffix = "..." if len(message) > _PREVIEW_CHARS else ""
    logger.info(
        "[whatsapp-stub] to=%s len=%d | %s%s",
        target,
        len(message),
        preview,
        suffix,
    )
