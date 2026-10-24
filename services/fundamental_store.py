"""Stored fundamentals: what the app serves while Yahoo refuses this server.

Rows are written by ``/api/internal/fundamentals`` (fed by the scheduled GitHub
Actions job, ``scripts/refresh_fundamentals.py``) and by any successful live
fetch. Every function a fundamentals request calls is best-effort: a database
problem is logged and treated as "nothing stored", so it can never break the
request. Only :func:`queue`, which serves the job, lets errors through.

Only stdlib is imported at module level, so the job can use :func:`key` and
:func:`clean` without the database libraries installed.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Asking again for a stock already requested within this window answers
# "being fetched" straight away, without another call to Yahoo.
REQUEST_RETRY_AFTER = timedelta(minutes=30)
# A request the job could never fill (Yahoo kept failing) is dropped after this.
REQUEST_EXPIRES = timedelta(days=7)

_SYMBOL = re.compile(r"^[A-Z0-9&-]{1,20}$")


def key(trading_symbol: Any) -> Optional[str]:
    """The base NSE symbol ("SUZLON-EQ" -> "SUZLON"), or None if it isn't one."""
    s = str(trading_symbol or "").strip().upper()
    for suffix in ("-EQ", "-BE", ".NS"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s if _SYMBOL.match(s) else None


def clean(value: Any) -> Any:
    """Make a fundamentals result JSON-safe: numpy scalars to Python, NaN/inf to None."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()  # numpy scalar
        except Exception:
            return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def load(trading_symbol: str) -> Optional[dict]:
    """The stored fundamentals for a stock, with ``as_of``, or None."""
    k = key(trading_symbol)
    if not k:
        return None
    try:
        from db.engine import get_session
        from db.models import FundamentalSnapshot

        with get_session() as session:
            row = session.get(FundamentalSnapshot, k)
            data_json, fetched_at = (row.data_json, row.fetched_at) if row else (None, None)
    except Exception as e:
        logger.warning("fundamental store: read failed for %s: %s", k, e)
        return None
    if not data_json:
        return None
    data = json.loads(data_json)
    data["symbol"] = trading_symbol
    data["as_of"] = _iso(fetched_at)
    return data


def requested_recently(trading_symbol: str, now: Optional[datetime] = None) -> bool:
    """Whether this stock is already waiting for the job (asked within the window)."""
    k = key(trading_symbol)
    if not k:
        return False
    now = now or _now()
    try:
        from db.engine import get_session
        from db.models import FundamentalSnapshot

        with get_session() as session:
            row = session.get(FundamentalSnapshot, k)
            requested_at = row.requested_at if row and not row.data_json else None
    except Exception as e:
        logger.warning("fundamental store: read failed for %s: %s", k, e)
        return False
    return requested_at is not None and now - _aware(requested_at) < REQUEST_RETRY_AFTER


def request(trading_symbol: str) -> bool:
    """Queue a stock for the job. True if the request was recorded."""
    k = key(trading_symbol)
    if not k:
        return False
    try:
        from db.engine import get_session
        from db.models import FundamentalSnapshot

        with get_session() as session:
            row = session.get(FundamentalSnapshot, k) or FundamentalSnapshot(symbol=k)
            row.requested_at = _now()
            session.add(row)
            session.commit()
        return True
    except Exception as e:
        logger.warning("fundamental store: could not queue %s: %s", k, e)
        return False


def save(result: dict) -> bool:
    """Store a successful fetch, replacing any earlier one. True if written."""
    k = key(result.get("symbol"))
    if not k or result.get("error") is not None:
        return False
    data = {f: v for f, v in result.items() if f not in ("as_of", "retryable")}
    try:
        from db.engine import get_session
        from db.models import FundamentalSnapshot

        with get_session() as session:
            row = session.get(FundamentalSnapshot, k) or FundamentalSnapshot(symbol=k)
            row.data_json = json.dumps(clean(data))
            row.fetched_at = _now()
            row.requested_at = None
            session.add(row)
            session.commit()
        return True
    except Exception as e:
        logger.warning("fundamental store: could not save %s: %s", k, e)
        return False


def forget_requests(symbols: Iterable[str]) -> int:
    """Drop queued requests (rows without data) for stocks Yahoo has no data for."""
    keys = {k for k in (key(s) for s in symbols) if k}
    if not keys:
        return 0
    try:
        from db.engine import get_session
        from db.models import FundamentalSnapshot

        dropped = 0
        with get_session() as session:
            for k in keys:
                row = session.get(FundamentalSnapshot, k)
                if row is not None and not row.data_json:
                    session.delete(row)
                    dropped += 1
            session.commit()
        return dropped
    except Exception as e:
        logger.warning("fundamental store: could not drop requests: %s", e)
        return 0


def queue(now: Optional[datetime] = None) -> dict:
    """What the job should know: stocks users are waiting for, and what is stored.

    ``wanted`` is oldest request first. Requests older than
    :data:`REQUEST_EXPIRES` are deleted here instead of being returned.
    """
    now = now or _now()
    from sqlmodel import select

    from db.engine import get_session
    from db.models import FundamentalSnapshot

    wanted: list[tuple[datetime, str]] = []
    stored: dict[str, Optional[str]] = {}
    with get_session() as session:
        for row in session.exec(select(FundamentalSnapshot)).all():
            if row.data_json:
                stored[row.symbol] = _iso(row.fetched_at)
            elif row.requested_at is None or now - _aware(row.requested_at) > REQUEST_EXPIRES:
                session.delete(row)
            else:
                wanted.append((_aware(row.requested_at), row.symbol))
        session.commit()
    return {"wanted": [s for _, s in sorted(wanted)], "stored": stored}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """SQLite hands timestamps back naive; they were written as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return _aware(dt).isoformat() if dt else None
