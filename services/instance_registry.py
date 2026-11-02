"""Detects when more than one server process is running.

The app keeps broker sessions, single-flight logins, rate limits and caches in
process memory, so it is only correct as a single instance (one Render
instance, one uvicorn worker). Each process upserts a row in ``app_instances``
every minute; if more than one has been live for longer than a deploy overlap,
``MULTIPLE_INSTANCES_DETECTED`` is logged at ERROR, at most hourly.

It only reports. Refusing to start would break Render's zero-downtime deploys,
where the old and new instance briefly overlap.
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from typing import Callable, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

BEAT_SECONDS = 60
#: An instance counts as live if it has beaten this recently.
LIVE_WINDOW_SECONDS = 180
#: Longer than any deploy overlap.
ALERT_AFTER_SECONDS = 15 * 60
ALERT_EVERY_SECONDS = 3600
PRUNE_AFTER_SECONDS = 24 * 3600

_UPSERT = text(
    "INSERT INTO app_instances (instance_id, started_at, last_seen_at) "
    "VALUES (:id, clock_timestamp(), clock_timestamp()) "
    "ON CONFLICT (instance_id) DO UPDATE SET last_seen_at = clock_timestamp()"
)
_COUNT_LIVE = text(
    "SELECT count(*) FROM app_instances "
    "WHERE last_seen_at > clock_timestamp() - make_interval(secs => :window)"
)
_PRUNE = text(
    "DELETE FROM app_instances "
    "WHERE last_seen_at < clock_timestamp() - make_interval(secs => :age)"
)
_DELETE_SELF = text("DELETE FROM app_instances WHERE instance_id = :id")


def new_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class InstanceMonitor:
    def __init__(
        self,
        engine,
        instance_id: Optional[str] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self.instance_id = instance_id or new_instance_id()
        self._clock = clock
        self._multiple_since: Optional[float] = None
        self._last_alert: Optional[float] = None

    def beat(self) -> int:
        """Record this instance, prune stale rows, and return the live count.

        PostgreSQL only (it relies on ``clock_timestamp()``); elsewhere it is a
        no-op that reports a single instance.
        """
        if self._engine.dialect.name != "postgresql":
            return 1
        with self._engine.begin() as c:
            c.execute(_UPSERT, {"id": self.instance_id})
            c.execute(_PRUNE, {"age": PRUNE_AFTER_SECONDS})
            live = int(c.execute(_COUNT_LIVE, {"window": LIVE_WINDOW_SECONDS}).scalar())
        self.evaluate(live)
        return live

    def evaluate(self, live: int) -> bool:
        """Track how long several instances have been live; alert when warranted.

        Returns True if an alert was logged on this call.
        """
        now = self._clock()
        if live <= 1:
            self._multiple_since = None
            return False
        if self._multiple_since is None:
            self._multiple_since = now
        if now - self._multiple_since < ALERT_AFTER_SECONDS:
            return False
        if self._last_alert is not None and now - self._last_alert < ALERT_EVERY_SECONDS:
            return False
        self._last_alert = now
        logger.error(
            "MULTIPLE_INSTANCES_DETECTED live=%d for %ds (this=%s). The app keeps "
            "sessions and limits in memory and must run as a single instance.",
            live, int(now - self._multiple_since), self.instance_id,
        )
        return True

    def stop(self) -> None:
        """Remove this instance's row on clean shutdown."""
        if self._engine.dialect.name != "postgresql":
            return
        with self._engine.begin() as c:
            c.execute(_DELETE_SELF, {"id": self.instance_id})
