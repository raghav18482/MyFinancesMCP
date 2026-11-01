"""Which client IP address to believe, and for what.

Every request reaches the app through a proxy: through Vercel's rewrite of
agentfolio.in, or directly at the Render host. ``X-Forwarded-For`` is a list the
*client* can start; only the entries written by our own proxies are facts.
Vercel documents that it overwrites the header with the visitor's address;
Render's behaviour is not documented consistently, so it is measured first
(``/__diag/ip``, see :mod:`web.routers.ip_diag`) before anything relies on it.

Two functions, for two purposes:

- :func:`trusted_client_ip` is for **security decisions** (per-IP login and
  deletion throttles, admin source allowlists). It returns an address only
  under a verified ``CLIENT_IP_MODE``, and ``None`` otherwise. Callers must treat
  ``None`` as "unknown", skip the per-IP check, and rely on per-identifier and
  global caps. Anything that *requires* a known source must refuse.
- :func:`abuse_client_ip` is a best-effort value (the leftmost entry, as the app
  always used) for **abuse limits** whose bypass costs nothing worse than spam,
  such as the feedback form. Its maps must be bounded.

Modes (``CLIENT_IP_MODE``):

``shadow`` (default)
    No trusted address. Used until the measurement settles which entry our
    proxy writes.
``render_last`` / ``render_first``
    Trust the last or first ``X-Forwarded-For`` entry, whichever the
    measurement showed Render writes itself.
``edge_secret``
    A request carrying ``X-Edge-Auth`` equal to ``EDGE_AUTH_SECRET`` came
    through our Vercel middleware, so its leftmost entry is the Vercel-written
    visitor address. Any other request falls back to the last entry.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

MODES = ("shadow", "render_last", "render_first", "edge_secret")

_warned_modes: set[str] = set()


def _mode() -> str:
    mode = (os.environ.get("CLIENT_IP_MODE") or "shadow").strip().lower()
    if mode not in MODES:
        if mode not in _warned_modes:
            _warned_modes.add(mode)
            logger.error("CLIENT_IP_MODE=%r is not one of %s; treating as shadow", mode, MODES)
        return "shadow"
    return mode


def current_mode() -> str:
    """The effective ``CLIENT_IP_MODE`` (unknown values count as ``shadow``)."""
    return _mode()


def _header(headers: Mapping[str, str], name: str) -> str:
    """``headers`` is Starlette's case-insensitive Headers or :func:`scope_headers`."""
    return headers.get(name) or ""


def forwarded_chain(headers: Mapping) -> list[str]:
    """The ``X-Forwarded-For`` entries, in order, with blanks dropped."""
    raw = _header(headers, "x-forwarded-for")
    return [part.strip() for part in raw.split(",") if part.strip()]


def abuse_client_ip(headers: Mapping, peer: Optional[str]) -> str:
    """Best-effort address for spam limits. Forgeable; never use it for security."""
    chain = forwarded_chain(headers)
    if chain:
        return chain[0]
    return peer or "unknown"


def trusted_client_ip(headers: Mapping, peer: Optional[str] = None) -> Optional[str]:
    """The client address our own proxies vouch for, or ``None`` if unverified."""
    mode = _mode()
    if mode == "shadow":
        return None
    chain = forwarded_chain(headers)
    if not chain:
        return None
    if mode == "render_last":
        return chain[-1]
    if mode == "render_first":
        return chain[0]
    # edge_secret
    secret = (os.environ.get("EDGE_AUTH_SECRET") or "").strip()
    presented = _header(headers, "x-edge-auth").strip()
    if secret and presented and hmac.compare_digest(presented.encode(), secret.encode()):
        return chain[0]
    return chain[-1]


def scope_headers(scope) -> dict[str, str]:
    """Lower-cased header dict from an ASGI scope."""
    return {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers") or []
    }


def scope_peer(scope) -> Optional[str]:
    client = scope.get("client")
    return client[0] if client else None
