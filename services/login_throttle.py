"""Attempt limiting for the app login form.

Before stored credentials existed, guessing the login form was pointless: an
attacker would have had to supply a working Angel One API key, client id, PIN
and TOTP secret, which is the broker's problem rather than ours. Now a single
app password unlocks a live trading account, so the form is worth brute-forcing
and has to be rate limited.

Two counters, both required:

- **per identifier** — stops someone grinding one known WhatsApp number from a
  pool of addresses.
- **per IP** — stops someone sweeping many numbers from one host, which the
  identifier counter alone would never notice.

In-memory and single-process, mirroring :mod:`session_manager` and the feedback
limiter. That is the honest scope: behind multiple workers each process keeps
its own tally, so the effective limit multiplies by the worker count. Move both
dicts to the database or Key Value if this ever runs on more than one process.

:func:`db.passwords.verify_password` is the second half of this defence — it
costs ~100 ms per guess, so even an unlimited attacker is slow.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_WINDOW_SEC = 15 * 60  # attempts older than this stop counting
_MAX_PER_IDENTIFIER = 5
_MAX_PER_IP = 20

_lock = threading.Lock()
_by_identifier: dict[str, list[float]] = {}
_by_ip: dict[str, list[float]] = {}


class TooManyAttempts(Exception):
    """Raised instead of checking the password, when a caller is over the cap."""

    def __init__(self, retry_after_sec: int) -> None:
        minutes = max(1, round(retry_after_sec / 60))
        super().__init__(
            f"Too many login attempts. Try again in about {minutes} minute"
            f"{'s' if minutes != 1 else ''}."
        )
        self.retry_after_sec = retry_after_sec


def check(identifier: str, client_ip: str) -> None:
    """Raise :class:`TooManyAttempts` if this login should not even be tried.

    Call before verifying the password. Attempts are recorded here rather than
    in :func:`record_failure` so that a flood of requests counts even if each
    one dies somewhere else in the handler.
    """
    now = time.time()
    key = (identifier or "").strip().lower()

    with _lock:
        ident_hits = _fresh(_by_identifier, key, now)
        ip_hits = _fresh(_by_ip, client_ip, now)

        for hits, cap in ((ident_hits, _MAX_PER_IDENTIFIER), (ip_hits, _MAX_PER_IP)):
            if len(hits) >= cap:
                # Oldest attempt in the window decides when the caller is free.
                retry_after = int(_WINDOW_SEC - (now - hits[0])) + 1
                logger.warning(
                    "login throttled: ip=%s identifier_attempts=%d ip_attempts=%d",
                    client_ip,
                    len(ident_hits),
                    len(ip_hits),
                )
                raise TooManyAttempts(retry_after)

        ident_hits.append(now)
        ip_hits.append(now)
        _by_identifier[key] = ident_hits
        _by_ip[client_ip] = ip_hits


def clear(identifier: str, client_ip: str) -> None:
    """Forget the attempt history after a successful login.

    Without this, five legitimate fumbled passwords would lock someone out for
    the rest of the window even once they got it right.
    """
    key = (identifier or "").strip().lower()
    with _lock:
        _by_identifier.pop(key, None)
        _by_ip.pop(client_ip, None)


def _fresh(store: dict[str, list[float]], key: str, now: float) -> list[float]:
    """Attempts for ``key`` still inside the window, oldest first."""
    return [t for t in store.get(key, []) if now - t < _WINDOW_SEC]
