"""WhatsApp delivery via the internal webhook service.

API contract:
    POST https://whatsapp-webhook-service-fczs.onrender.com/send
    Content-Type: application/json
    Authorization: Bearer <WHATSAPP_WEBHOOK_TOKEN>   (sent when configured)
    {"phone": "910000000000", "messages": ["<text>"]}

Phone normalisation:
    - Strip everything but digits.
    - If the resulting string is 10 digits, prepend "91".
    - Result must be 12 digits (e.g. 910000000000).

Outcomes
--------
Every send ends in exactly one of three outcomes, and only a certain "not
sent" is ever retried:

``accepted``
    A 2xx: the webhook handed the message to WhatsApp, which accepted it. That
    is not confirmation the recipient received it; nothing here can know that.
    :func:`_post` returns normally.
``not_sent`` (:class:`DeliveryNotSent`)
    Certainly not sent: the connection was never made, the webhook answered
    503 *and* said "not ready, nothing sent" (its ``X-WWS-Outcome: not_sent``
    header plus the not-connected error), or it rejected the request (4xx,
    returned before any send). Connection failures and the not-ready 503 are
    retried every ``_RETRY_DELAY_SEC`` until ``_SEND_DEADLINE_SEC``; the webhook
    sleeps when idle and answers 503 while it reconnects.
``unknown`` (:class:`DeliveryUnknown`)
    It may or may not have been sent: a read timeout, a 500 (the send can fail
    after a partial send), any other 5xx, a 503 without the webhook's own
    "not sent" signal (for example from Render's edge), a dropped connection,
    or a response whose status and outcome disagree. **Never retried**: a retry
    could deliver the message twice.

Only :mod:`services.schedular.briefing_delivery` and the feedback reply may call
:func:`_post` (``tests/test_app_wiring.py`` checks), so every briefing goes
through one claim-checked path.
"""
from __future__ import annotations

import asyncio
import logging
import os
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
_DIGITS_RE = re.compile(r"\D+")

#: The webhook's own "not ready" answer (WWS index.js), returned before any send.
_NOT_READY_ERROR = "WhatsApp not connected yet"
_OUTCOME_HEADER = "x-wws-outcome"

ACCEPTED = "accepted"
NOT_SENT = "not_sent"
UNKNOWN = "unknown"


class DeliveryError(RuntimeError):
    """A send that did not end in ``accepted``. ``reason`` is a short code."""

    outcome = UNKNOWN

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"whatsapp {self.outcome} ({reason}){': ' + detail if detail else ''}")
        self.reason = reason


class DeliveryNotSent(DeliveryError):
    """Certainly not sent."""

    outcome = NOT_SENT


class DeliveryUnknown(DeliveryError):
    """May or may not have been sent. Must never be retried automatically."""

    outcome = UNKNOWN


def _normalise_phone(phone: str | None) -> str:
    """Return a 12-digit number (91XXXXXXXXXX).

    Accepts any of: "0000000000", "+910000000000", "91 0000 000000", etc.
    Raises ValueError when the result isn't exactly 12 digits. The message does
    not repeat the number.
    """
    digits = _DIGITS_RE.sub("", phone or "")
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) != 12:
        raise ValueError(f"Cannot normalise the phone number to 12 digits (got {len(digits)})")
    return digits


def _mask(phone: str) -> str:
    return f"***{phone[-4:]}" if len(phone) > 4 else "***"


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def classify(resp: httpx.Response) -> tuple[str, str]:
    """``(outcome, reason)`` for one webhook response. Public for tests."""
    header = (resp.headers.get(_OUTCOME_HEADER) or "").strip().lower()
    body = _json(resp)
    said = str(body.get("outcome") or "").strip().lower()
    status = resp.status_code

    if 200 <= status < 300:
        # A 2xx is the webhook saying sendMessage completed. If it also states
        # an outcome, the statement must agree.
        if header not in ("", ACCEPTED) or said not in ("", ACCEPTED):
            return UNKNOWN, "inconsistent"
        return ACCEPTED, "accepted"
    if status == 503:
        error = str(body.get("error") or "")
        if header == NOT_SENT and said in ("", NOT_SENT) and error.startswith(_NOT_READY_ERROR):
            return NOT_SENT, "not_ready"
        return UNKNOWN, "http_503"
    if 400 <= status < 500:
        # The webhook validates (and, once enabled, authenticates) before it
        # sends anything, so a 4xx means nothing went out.
        if header not in ("", NOT_SENT):
            return UNKNOWN, "inconsistent"
        return NOT_SENT, "auth" if status in (401, 403) else "http_4xx"
    return UNKNOWN, f"http_{status // 100}xx"


async def _post(phone: str | None, message: str) -> None:
    """Send ``message`` to ``phone``. Returns on ``accepted``; raises otherwise.

    Raises :class:`DeliveryNotSent` (certainly not sent) or
    :class:`DeliveryUnknown` (may have been sent; never retry it). A phone that
    cannot be normalised raises :class:`DeliveryNotSent` too.
    """
    try:
        to = _normalise_phone(phone)
    except ValueError as e:
        raise DeliveryNotSent("bad_phone", str(e)) from None

    payload = {"phone": to, "messages": [message]}
    headers = {"Content-Type": "application/json"}
    token = (os.environ.get("WHATSAPP_WEBHOOK_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    deadline = time.monotonic() + _SEND_DEADLINE_SEC
    attempt = 0
    last: DeliveryError = DeliveryNotSent("deadline")
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
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # No connection, so no request reached the webhook: safe to retry.
                last = DeliveryNotSent("connect", type(exc).__name__)
            except httpx.TransportError as exc:
                # Read timeout, dropped connection, partial write: the webhook may
                # have received the request and sent the message.
                raise DeliveryUnknown("transport", type(exc).__name__) from None
            else:
                outcome, reason = classify(resp)
                if outcome == ACCEPTED:
                    logger.info("[whatsapp] accepted for %s (attempt %d)", _mask(to), attempt)
                    return
                if outcome == UNKNOWN:
                    raise DeliveryUnknown(reason, f"HTTP {resp.status_code}")
                if reason != "not_ready":
                    raise DeliveryNotSent(reason, f"HTTP {resp.status_code}")
                last = DeliveryNotSent(reason, "webhook not ready")

            logger.warning("[whatsapp] attempt %d not sent: %s", attempt, last.reason)
            if deadline - time.monotonic() <= _RETRY_DELAY_SEC:
                break
            await asyncio.sleep(_RETRY_DELAY_SEC)

    raise last
