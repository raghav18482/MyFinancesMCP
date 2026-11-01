"""Failed admin-key attempts: limited per source, watched globally, never a lockout.

A correct key from an allowed source is checked first and always succeeds, so
nobody can lock the admin out by flooding wrong keys. Failures are only:

- capped per source (``PER_SOURCE_MAX`` in ``PER_SOURCE_WINDOW_SEC``, then 429
  for that source alone), and
- counted globally: above ``GLOBAL_ALERT_PER_HOUR`` an ``ADMIN_AUTH_FAILURES_HIGH``
  alert is logged (at most every ``ALERT_EVERY_SEC``) and *failed* responses are
  delayed by ``FAILURE_DELAY_SEC``.

The key itself is at least 32 random characters, so these limits reduce noise;
they are not what makes guessing impractical. In-memory and per process, like
the other limiters (the app runs as one instance).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PER_SOURCE_MAX = 10
PER_SOURCE_WINDOW_SEC = 15 * 60
GLOBAL_ALERT_PER_HOUR = 50
ALERT_EVERY_SEC = 600
FAILURE_DELAY_SEC = 1.0
MAX_SOURCES = 10_000


class AdminGuard:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._by_source: OrderedDict[str, deque[float]] = OrderedDict()
        self._global: deque[float] = deque()
        self._last_alert: Optional[float] = None

    def retry_after(self, source: str) -> Optional[int]:
        """Seconds until ``source`` may try again, or ``None`` if it may now."""
        now = self._clock()
        with self._lock:
            hits = self._fresh(source, now)
            if len(hits) >= PER_SOURCE_MAX:
                return int(PER_SOURCE_WINDOW_SEC - (now - hits[0])) + 1
            return None

    def record_failure(self, source: str) -> float:
        """Count a failed attempt; return how long to delay the failed response."""
        now = self._clock()
        alert = False
        with self._lock:
            hits = self._fresh(source, now)
            hits.append(now)
            self._by_source[source] = hits
            self._by_source.move_to_end(source)
            while len(self._by_source) > MAX_SOURCES:
                self._by_source.popitem(last=False)

            self._global.append(now)
            while self._global and now - self._global[0] >= 3600:
                self._global.popleft()
            high = len(self._global) > GLOBAL_ALERT_PER_HOUR
            if high and (self._last_alert is None or now - self._last_alert >= ALERT_EVERY_SEC):
                self._last_alert = now
                alert = True
            count = len(self._global)
        if alert:
            logger.error("ADMIN_AUTH_FAILURES_HIGH: %d failed admin-key attempts in the last hour", count)
        return FAILURE_DELAY_SEC if high else 0.0

    def reset(self) -> None:
        with self._lock:
            self._by_source.clear()
            self._global.clear()
            self._last_alert = None

    def _fresh(self, source: str, now: float) -> deque[float]:
        hits = self._by_source.get(source) or deque()
        while hits and now - hits[0] >= PER_SOURCE_WINDOW_SEC:
            hits.popleft()
        return hits


guard = AdminGuard()
