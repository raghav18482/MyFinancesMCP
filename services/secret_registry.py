"""Exact-value redaction for secrets that can reach a log line without a key name.

Pattern-based redaction in :mod:`services.logging_config` catches ``password=...`` or
``Bearer ...``, but not a bare value: an exception whose message is just the
PIN, or an object whose ``repr`` prints the API key. So every live broker
credential and token is registered here, against an owner (one broker client),
and the log filter replaces any exact occurrence of a registered value.

Bounded on purpose. An attacker who can trigger failed logins must not be able
to grow this without limit, or flush out real secrets by filling it with junk:
values are capped, the oldest owners are evicted first, entries expire after a
broker session's lifetime, and failed logins release their values immediately
(see :meth:`session_manager.SessionManager.create_session`).

Values live only in memory, where the broker client already holds them.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

#: Shorter values would redact ordinary words and numbers.
MIN_LENGTH = 4
#: Total values across all owners.
MAX_VALUES = 10_000
#: A broker session lasts 8 hours; keep values a little longer than that.
TTL_SECONDS = 9 * 3600


class SecretRegistry:
    def __init__(
        self,
        *,
        max_values: int = MAX_VALUES,
        ttl_seconds: float = TTL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self._max_values = max_values
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # owner -> (expires_at, values), oldest owner first
        self._owners: OrderedDict[str, tuple[float, frozenset[str]]] = OrderedDict()
        self._pattern: Optional[re.Pattern[str]] = None
        self._dirty = False

    def register(self, owner: str, *values: Optional[str]) -> None:
        """Add ``values`` under ``owner``. Non-strings and short values are ignored."""
        keep = frozenset(
            v for v in values if isinstance(v, str) and len(v.strip()) >= MIN_LENGTH
        )
        if not keep:
            return
        evicted = 0
        with self._lock:
            previous = self._owners.pop(owner, (0.0, frozenset()))[1]
            self._owners[owner] = (self._clock() + self._ttl, previous | keep)
            while self._count() > self._max_values and len(self._owners) > 1:
                self._owners.popitem(last=False)
                evicted += 1
            self._dirty = True
        # Logged outside the lock: the log filter calls back into redact().
        if evicted:
            logger.warning("SECRET_REGISTRY_FULL: evicted %d oldest owner(s)", evicted)

    def release(self, owner: str) -> None:
        with self._lock:
            if self._owners.pop(owner, None) is not None:
                self._dirty = True

    def redact(self, text: str) -> str:
        """``text`` with every registered value replaced by ``[REDACTED]``."""
        if not text:
            return text
        pattern = self._current_pattern()
        if pattern is None:
            return text
        return pattern.sub("[REDACTED]", text)

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return self._count()

    # ── internals (call with the lock held) ───────────────────────────────

    def _count(self) -> int:
        return sum(len(values) for _, values in self._owners.values())

    def _expire(self) -> None:
        now = self._clock()
        stale = [o for o, (expires, _) in self._owners.items() if expires <= now]
        for o in stale:
            del self._owners[o]
        if stale:
            self._dirty = True

    def _current_pattern(self) -> Optional[re.Pattern[str]]:
        with self._lock:
            self._expire()
            if self._dirty:
                self._pattern = _compile(
                    {v for _, values in self._owners.values() for v in values}
                )
                self._dirty = False
            return self._pattern


def _compile(values: set[str]) -> Optional[re.Pattern[str]]:
    if not values:
        return None
    parts = []
    # Longest first, so a value that contains another is replaced whole.
    for v in sorted(values, key=len, reverse=True):
        escaped = re.escape(v)
        # A numeric PIN must not match inside a longer number.
        parts.append(rf"(?<!\d){escaped}(?!\d)" if v.isdigit() else escaped)
    return re.compile("|".join(parts))


registry = SecretRegistry()


def register(owner: str, *values: Optional[str]) -> None:
    registry.register(owner, *values)


def release(owner: str) -> None:
    registry.release(owner)


def redact_known(text: str) -> str:
    return registry.redact(text)
