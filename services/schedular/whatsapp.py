"""WhatsApp delivery via the internal webhook service.

API contract:
    POST https://whatsapp-webhook-service-fczs.onrender.com/send
    Content-Type: application/json
    {"phone": "918107037133", "messages": ["<text>"]}

Phone normalisation:
    - Strip everything but digits.
    - If the resulting string is 10 digits, prepend "91".
    - Result must be 12 digits (e.g. 918107037133).

Retries:
    The webhook service runs on Render's free tier, which sleeps when idle. The
    first request after a nap takes ~30s while the instance boots, and then
    answers 503 "WhatsApp not connected yet" for a few seconds while it
    reconnects. So each attempt may wait up to ``_ATTEMPT_TIMEOUT_SEC``, and
    5xx / network errors are retried every ``_RETRY_DELAY_SEC`` until
    ``_SEND_DEADLINE_SEC`` runs out. 4xx is terminal.
Missing / blank phone raises RuntimeError so the scheduler logs the failure.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

_SEND_URL = "https://whatsapp-webhook-service-fczs.onrender.com/send"
_CONNECT_TIMEOUT_SEC = 10.0
# Long enough for a cold start (~30s observed) to finish within one attempt.
_ATTEMPT_TIMEOUT_SEC = 45.0
_RETRY_DELAY_SEC = 3.0
# Whole send, retries included. The public feedback form waits on this inside an
# HTTP request, so keep it well under proxy timeouts (Cloudflare cuts at 100s).
_SEND_DEADLINE_SEC = 60.0
# Tests swap in an httpx.MockTransport.
_transport: httpx.AsyncBaseTransport | None = None
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

    deadline = time.monotonic() + _SEND_DEADLINE_SEC
    last_error: str | None = None
    attempt = 0
    async with httpx.AsyncClient(transport=_transport) as client:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempt += 1
            read_timeout = min(_ATTEMPT_TIMEOUT_SEC, remaining)
            timeout = httpx.Timeout(read_timeout, connect=min(_CONNECT_TIMEOUT_SEC, read_timeout))
            try:
                resp = await client.post(_SEND_URL, json=payload, headers=headers, timeout=timeout)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = f"transport error: {exc!r}"
            else:
                if 200 <= resp.status_code < 300:
                    logger.info(
                        "[whatsapp] to=%s status=%d attempt=%d | %s",
                        to,
                        resp.status_code,
                        attempt,
                        _preview(message),
                    )
                    return
                if not 500 <= resp.status_code < 600:
                    raise RuntimeError(
                        f"whatsapp: {resp.status_code} {resp.text[:200]}"
                    )
                last_error = f"{resp.status_code} server error: {resp.text[:200]}"

            logger.warning("[whatsapp] attempt %d failed: %s", attempt, last_error)
            if deadline - time.monotonic() <= _RETRY_DELAY_SEC:
                break
            await asyncio.sleep(_RETRY_DELAY_SEC)

    raise RuntimeError(
        f"whatsapp: send failed after {attempt} attempt(s) in {_SEND_DEADLINE_SEC:.0f}s ({last_error})"
    )
