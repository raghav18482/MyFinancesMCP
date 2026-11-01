"""Temporary measurement of the proxy chain, to pick ``CLIENT_IP_MODE``.

``GET /__diag/ip?k=<token>`` returns the caller's own ``X-Forwarded-For``
entries and the TCP peer the app sees. Calling it through agentfolio.in and
through the Render host, with and without a forged header, shows which entry our
proxies write (see :mod:`web.client_ip`).

Off unless both are set:

- ``IP_DIAG_TOKEN``: the value ``k`` must match (constant-time).
- ``IP_DIAG_UNTIL``: an ISO-8601 UTC time no more than 2 hours ahead. After it,
  or if it is further out than that, the endpoint is a 404.

Anything else is a 404, indistinguishable from the route not existing. This
router is removed once the measurement is done.
"""
from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from web.client_ip import forwarded_chain

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_WINDOW = timedelta(hours=2)
_NO_STORE = {"Cache-Control": "no-store"}


def _enabled(now: datetime) -> bool:
    token = (os.environ.get("IP_DIAG_TOKEN") or "").strip()
    until_raw = (os.environ.get("IP_DIAG_UNTIL") or "").strip()
    if not token or not until_raw:
        return False
    try:
        until = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("IP_DIAG_UNTIL is not an ISO-8601 time; diagnostics stay off")
        return False
    if until.tzinfo is None:
        return False
    if until - now > MAX_WINDOW:
        logger.warning("IP_DIAG_UNTIL is more than 2 hours ahead; diagnostics stay off")
        return False
    return now < until


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


@router.get("/__diag/ip")
async def ip_diag(request: Request, k: str = ""):
    if not _enabled(datetime.now(timezone.utc)):
        return _not_found()
    token = (os.environ.get("IP_DIAG_TOKEN") or "").strip()
    if not k or not hmac.compare_digest(k.encode(), token.encode()):
        return _not_found()
    headers = request.headers
    return JSONResponse(
        {
            "x_forwarded_for": forwarded_chain(headers),
            "peer": request.client.host if request.client else None,
            "x_real_ip": headers.get("x-real-ip"),
            "forwarded": headers.get("forwarded"),
            "vercel_headers": sorted(h for h in headers.keys() if h.startswith("x-vercel")),
        },
        headers=_NO_STORE,
    )
