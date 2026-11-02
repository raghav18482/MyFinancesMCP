"""Which client IP address to believe, and for what.

Every request reaches the app through a proxy: through Vercel's rewrite of
agentfolio.in, or directly at the Render host. ``X-Forwarded-For`` is a list the
*client* can start; only the entries written by our own proxies are facts.
Vercel documents that it overwrites the header with the visitor's address;
Render's behaviour is not documented consistently, so the mode is set only
from a measurement (``docs/runbooks/client-ip.md``), never by guessing.

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
    proxy writes, and the rollback for every other mode.
``render_last`` / ``render_first``
    Trust the last or first ``X-Forwarded-For`` entry, whichever the
    measurement showed Render writes itself.
``edge_secret``
    A request carrying ``X-Edge-Auth`` equal to ``EDGE_AUTH_SECRET`` came
    through our Vercel middleware, so its leftmost entry is the Vercel-written
    visitor address. Any other request falls back to the last entry.

The entry a mode picks must parse as an IP address, or the result is ``None``:
a malformed value never keys a security limit. In production a mode that is
not one of these, or ``edge_secret`` without a strong ``EDGE_AUTH_SECRET``,
stops startup (:func:`validate_config`).
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

MODES = ("shadow", "render_last", "render_first", "edge_secret")
MIN_EDGE_SECRET_CHARS = 32

_warned_modes: set[str] = set()


def _configured_mode() -> str:
    return (os.environ.get("CLIENT_IP_MODE") or "shadow").strip().lower()


def _edge_secret() -> str:
    return (os.environ.get("EDGE_AUTH_SECRET") or "").strip()


def _mode() -> str:
    mode = _configured_mode()
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
        return _as_ip(chain[-1])
    if mode == "render_first":
        return _as_ip(chain[0])
    # edge_secret
    secret = _edge_secret()
    presented = _header(headers, "x-edge-auth").strip()
    if secret and presented and hmac.compare_digest(presented.encode(), secret.encode()):
        return _as_ip(chain[0])
    return _as_ip(chain[-1])


def _as_ip(entry: str) -> Optional[str]:
    """``entry`` in canonical form if it is an IP address, else ``None``."""
    try:
        return str(ipaddress.ip_address(entry))
    except ValueError:
        return None


def validate_config(production: bool) -> None:
    """Raise ``ValueError`` on a client-IP setting production must not start with.

    Outside production an unknown mode only logs and counts as ``shadow``. In
    production it means someone meant to turn enforcement on and mistyped it,
    so the deploy fails rather than silently trusting nothing; likewise
    ``edge_secret`` with a missing or short secret, which would quietly trust
    only the Render hop.
    """
    if not production:
        return
    mode = _configured_mode()
    if mode not in MODES:
        raise ValueError(f"CLIENT_IP_MODE must be one of {', '.join(MODES)}")
    if mode == "edge_secret" and len(_edge_secret()) < MIN_EDGE_SECRET_CHARS:
        raise ValueError(
            f"CLIENT_IP_MODE=edge_secret needs EDGE_AUTH_SECRET of at least "
            f"{MIN_EDGE_SECRET_CHARS} characters"
        )


def log_status() -> None:
    """One startup line naming the effective mode. Never the secret."""
    mode = _mode()
    logger.info("CLIENT_IP mode=%s per_ip_security_limits=%s", mode,
                "off" if mode == "shadow" else "on")


def scope_headers(scope) -> dict[str, str]:
    """Lower-cased header dict from an ASGI scope."""
    return {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers") or []
    }


def scope_peer(scope) -> Optional[str]:
    client = scope.get("client")
    return client[0] if client else None
