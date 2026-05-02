"""WhatsApp delivery via the internal webhook service.

API contract:
    POST https://whatsapp-webhook-service-fczs.onrender.com/send
    Content-Type: application/json
    {"phone": "918107037133", "messages": ["<text>"]}

Phone normalisation:
    - Strip everything but digits.
    - If the resulting string is 10 digits, prepend "91".
    - Result must be 12 digits (e.g. 918107037133).

One retry on 5xx / network errors; 4xx is terminal.
Missing / blank phone raises RuntimeError so the scheduler logs the failure.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_SEND_URL = "https://whatsapp-webhook-service-fczs.onrender.com/send"
_TIMEOUT_SEC = 15.0
_RETRY_DELAY_SEC = 1.5
_PREVIEW_CHARS = 140
_DIGITS_RE = re.compile(r"\D+")


def _normalise_phone(phone: str | None) -> str:
    """Return a 12-digit number (91XXXXXXXXXX).

    Accepts any of: "8107037133", "+918107037133", "91 8107 037133", etc.
    Raises ValueError when the result isn't exactly 12 digits.
    """
    digits = _DIGITS_RE.sub("", phone or "")
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) != 12:
        raise ValueError(
            f"Cannot normalise phone '{phone}' to 12 digits (got '{digits}')"
        )
    return digits


def _preview(message: str) -> str:
    line = message.replace("\n", " \u23ce ")[:_PREVIEW_CHARS]
    return line + ("..." if len(message) > _PREVIEW_CHARS else "")


async def send(phone: str | None, message: str) -> None:
    """Send ``message`` to ``phone`` via the webhook service.

    Raises ``RuntimeError`` on unrecoverable failure so the scheduler
    records the error in the ``logs`` table.
    """
    to = _normalise_phone(phone)

    payload = {"phone": to, "messages": [message]}
    headers = {"Content-Type": "application/json"}

    last_error: str | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(_TIMEOUT_SEC)) as client:
        for attempt in (1, 2):
            try:
                resp = await client.post(_SEND_URL, json=payload, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = f"transport error: {exc}"
                if attempt == 1:
                    await asyncio.sleep(_RETRY_DELAY_SEC)
                    continue
                break

            if 200 <= resp.status_code < 300:
                logger.info(
                    "[whatsapp] to=%s status=%d | %s",
                    to,
                    resp.status_code,
                    _preview(message),
                )
                return

            if 500 <= resp.status_code < 600:
                last_error = f"{resp.status_code} server error: {resp.text[:200]}"
                if attempt == 1:
                    await asyncio.sleep(_RETRY_DELAY_SEC)
                    continue
                break

            raise RuntimeError(
                f"whatsapp: {resp.status_code} {resp.text[:200]}"
            )

    raise RuntimeError(f"whatsapp: send failed after retry ({last_error})")
