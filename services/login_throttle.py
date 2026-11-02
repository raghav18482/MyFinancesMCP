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

#: Above this many keys a map is swept of keys with no attempt in the window.
#: Keys still inside the window are never evicted: dropping one would let a
#: flood of new keys reset someone's count.
_PRUNE_ABOVE = 10_000

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


def check(identifier: str, client_ip: str | None) -> None:
    """Raise :class:`TooManyAttempts` if this login should not even be tried.

    Call before verifying the password. Attempts are recorded here rather than
    in :func:`record_failure` so that a flood of requests counts even if each
    one dies somewhere else in the handler.

    ``client_ip`` must come from :func:`web.dependencies.trusted_ip`. When it is
    ``None`` (the address cannot be verified yet) only the per-identifier cap
    applies: a forgeable address must never key a security limit.
    """
    now = time.time()
    key = (identifier or "").strip().lower()

    with _lock:
        ident_hits = _fresh(_by_identifier, key, now)
        caps = [(ident_hits, _MAX_PER_IDENTIFIER)]
        ip_hits: list[float] = []
        if client_ip is not None:
            ip_hits = _fresh(_by_ip, client_ip, now)
            caps.append((ip_hits, _MAX_PER_IP))

        for hits, cap in caps:
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
        _by_identifier[key] = ident_hits
        if client_ip is not None:
            ip_hits.append(now)
            _by_ip[client_ip] = ip_hits
        for store in (_by_identifier, _by_ip):
            if len(store) > _PRUNE_ABOVE:
                _prune(store, now)


def clear(identifier: str, client_ip: str | None) -> None:
    """Forget the attempt history after a successful login.

    Without this, five legitimate fumbled passwords would lock someone out for
    the rest of the window even once they got it right.
    """
    key = (identifier or "").strip().lower()
    with _lock:
        _by_identifier.pop(key, None)
        if client_ip is not None:
            _by_ip.pop(client_ip, None)


_by_global: dict[str, list[float]] = {}


def check_global(name: str, cap: int) -> None:
    """Raise :class:`TooManyAttempts` once ``cap`` attempts named ``name`` happened
    in the window, across all callers.

    For actions where per-identifier caps are not enough because the attacker
    picks the identifier, such as enrollment with someone else's client id.
    """
    now = time.time()
    with _lock:
        hits = _fresh(_by_global, name, now)
        if len(hits) >= cap:
            retry_after = int(_WINDOW_SEC - (now - hits[0])) + 1
            logger.warning("global throttle %s reached (%d attempts)", name, len(hits))
            raise TooManyAttempts(retry_after)
        hits.append(now)
        _by_global[name] = hits


def _prune(store: dict[str, list[float]], now: float) -> None:
    """Drop keys whose newest attempt has left the window. Caller holds ``_lock``."""
    for key in [k for k, v in store.items() if not v or now - v[-1] >= _WINDOW_SEC]:
        del store[key]


def _fresh(store: dict[str, list[float]], key: str, now: float) -> list[float]:
    """Attempts for ``key`` still inside the window, oldest first."""
    return [t for t in store.get(key, []) if now - t < _WINDOW_SEC]
