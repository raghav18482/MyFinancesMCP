"""Last-known-good fundamentals, in Postgres so they survive deploys.

Read before Yahoo (a fresh-enough row means no Yahoo call at all) and after a
Yahoo failure (an older row is served, marked stale, instead of an error).
Written after every successful fetch, by the app or by
``scripts/refresh_fundamentals.py`` run from a machine Yahoo does not block.

Strictly best-effort: the database being unreachable must never break a
fundamentals request. After a failure the store stays off for a minute rather
than adding a connection timeout to every request.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Serve without asking Yahoo while the snapshot is younger than this.
FRESH_FOR_SEC = int(os.environ.get("FUNDAMENTALS_FRESH_SEC", 24 * 3600))
# Serve as stale, when Yahoo fails, while younger than this.
STALE_FOR_SEC = int(os.environ.get("FUNDAMENTALS_STALE_MAX_SEC", 45 * 24 * 3600))

_ENABLED = os.environ.get("FUNDAMENTALS_STORE", "on").lower() not in {"off", "0", "false"}
_PAUSE_AFTER_ERROR_SEC = 60
_lock = threading.Lock()
_paused_until = 0.0
_last_error: str | None = None


def _available() -> bool:
    return _ENABLED and time.time() >= _paused_until


def _pause(e: Exception) -> None:
    global _paused_until, _last_error
    with _lock:
        first = time.time() >= _paused_until
        _paused_until = time.time() + _PAUSE_AFTER_ERROR_SEC
        _last_error = f"{type(e).__name__}: {e}"[:300]
    if first:
        logger.warning("fundamentals store unavailable, pausing %ds: %s",
                       _PAUSE_AFTER_ERROR_SEC, _last_error)


def load(symbol: str) -> tuple[dict, datetime] | None:
    """The stored result for ``symbol`` and when it was fetched, or None."""
    if not _available():
        return None
    try:
        from db import get_session
        from db.models import FundamentalSnapshot

        with get_session() as session:
            row = session.get(FundamentalSnapshot, symbol)
            if row is None:
                return None
            fetched = row.fetched_at
            if fetched.tzinfo is None:  # sqlite drops tzinfo
                fetched = fetched.replace(tzinfo=timezone.utc)
            return json.loads(row.payload_json), fetched
    except Exception as e:
        _pause(e)
        return None


def save(symbol: str, result: dict) -> bool:
    """Upsert ``result`` as the latest good snapshot for ``symbol``."""
    if not _available():
        return False
    try:
        from db import get_session
        from db.models import FundamentalSnapshot, utcnow

        payload = json.dumps(result, default=str)
        with get_session() as session:
            row = session.get(FundamentalSnapshot, symbol)
            if row is None:
                row = FundamentalSnapshot(symbol=symbol, payload_json=payload)
            else:
                row.payload_json = payload
                row.fetched_at = utcnow()
            session.add(row)
            session.commit()
        return True
    except Exception as e:
        _pause(e)
        return False


def age_seconds(fetched_at: datetime) -> float:
    return (datetime.now(timezone.utc) - fetched_at).total_seconds()


def status() -> dict:
    """For the health endpoint: counts and freshness, no payloads."""
    info: dict = {"enabled": _ENABLED, "available": _available(), "last_error": _last_error}
    if not _available():
        return info
    try:
        from sqlalchemy import func
        from sqlmodel import select

        from db import get_session
        from db.models import FundamentalSnapshot

        with get_session() as session:
            count = session.exec(select(func.count()).select_from(FundamentalSnapshot)).one()
            newest = session.exec(select(func.max(FundamentalSnapshot.fetched_at))).one()
        info.update(symbols=count, newest=str(newest) if newest else None)
    except Exception as e:
        _pause(e)
        info.update(available=False, last_error=_last_error)
    return info
