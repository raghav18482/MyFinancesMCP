"""Analyst estimates, revision momentum and earnings surprise.

Estimate revision momentum — whether analysts are raising or cutting their
numbers — is one of the better-documented equity signals, and the repo had
nothing like it: ``fundamental_service`` carries reported revenue and profit
history but no forward view at all.

All of this comes from yfinance, which is already pinned. Verified available
for NSE tickers: ``earnings_estimate``, ``revenue_estimate``, ``eps_trend``,
``eps_revisions``, ``growth_estimates``, ``analyst_price_targets``,
``earnings_dates``.

``eps_trend`` is the useful one. It reports the current consensus EPS for each
forward period alongside what that consensus was 7, 30, 60 and 90 days ago, so
revision momentum is a subtraction rather than a time series you have to store.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_cache: dict[str, dict] = {}
_CACHE_TTL = 6 * 3600

# Columns yfinance uses in eps_trend, in order of recency.
_TREND_COLUMNS = ["current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo"]


def get_estimates(trading_symbol: str) -> dict:
    """Forward estimates, revision momentum and recent surprises for one stock."""
    cache_key = f"estimates:{trading_symbol}"
    entry = _cache.get(cache_key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]

    import yfinance as yf

    yf_symbol = _yf_symbol(trading_symbol)
    result: dict[str, Any] = {
        "symbol": trading_symbol,
        "yf_symbol": yf_symbol,
        "consensus": {},
        "revisions": {},
        "surprises": [],
        "next_result": None,
        "price_target": {},
        "error": None,
    }

    try:
        ticker = yf.Ticker(yf_symbol)

        # Same parallel-fetch pattern as fundamental_service: each of these is
        # a separate network round trip and they are independent.
        props = {
            "eps_trend": lambda: ticker.eps_trend,
            "eps_revisions": lambda: ticker.eps_revisions,
            "earnings_estimate": lambda: ticker.earnings_estimate,
            "revenue_estimate": lambda: ticker.revenue_estimate,
            "earnings_dates": lambda: ticker.earnings_dates,
            "price_targets": lambda: ticker.analyst_price_targets,
        }
        fetched: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(fn): name for name, fn in props.items()}
            for future in futures:
                name = futures[future]
                try:
                    fetched[name] = future.result(timeout=20)
                except Exception as e:
                    logger.debug("estimates: '%s' failed for %s: %s", name, yf_symbol, e)
                    fetched[name] = None

        result["consensus"] = _consensus(fetched.get("earnings_estimate"),
                                         fetched.get("revenue_estimate"))
        result["revisions"] = _revision_momentum(fetched.get("eps_trend"),
                                                 fetched.get("eps_revisions"))
        result["surprises"], result["next_result"] = _earnings_history(
            fetched.get("earnings_dates"))
        result["price_target"] = _price_target(fetched.get("price_targets"))
        result["summary"] = _summarise(result)

    except Exception as e:
        logger.warning("estimates fetch failed for %s: %s", trading_symbol, e)
        result["error"] = str(e)

    _cache[cache_key] = {"data": result, "ts": time.time()}
    return result


def _consensus(earnings: Optional[pd.DataFrame],
               revenue: Optional[pd.DataFrame]) -> dict:
    """Current consensus EPS and revenue per forward period."""
    out: dict[str, Any] = {}

    if earnings is not None and not earnings.empty:
        for period, row in earnings.iterrows():
            out.setdefault(str(period), {})["eps"] = {
                "avg": _num(row.get("avg")),
                "low": _num(row.get("low")),
                "high": _num(row.get("high")),
                "analysts": _int(row.get("numberOfAnalysts")),
                "year_ago": _num(row.get("yearAgoEps")),
                "growth": _num(row.get("growth")),
            }

    if revenue is not None and not revenue.empty:
        for period, row in revenue.iterrows():
            out.setdefault(str(period), {})["revenue"] = {
                "avg": _num(row.get("avg")),
                "analysts": _int(row.get("numberOfAnalysts")),
                "growth": _num(row.get("growth")),
            }

    return out


def _revision_momentum(eps_trend: Optional[pd.DataFrame],
                       eps_revisions: Optional[pd.DataFrame]) -> dict:
    """How consensus EPS has moved over 7, 30, 60 and 90 days.

    Expressed as a percentage change from the older estimate to the current
    one, so a positive number means analysts are raising their numbers. The
    percentage is signed by the direction of the revision even when the older
    estimate was negative, which a naive ratio gets backwards.
    """
    out: dict[str, Any] = {"by_period": {}, "direction": "unknown"}

    if eps_trend is None or eps_trend.empty:
        return out

    overall: list[float] = []

    for period, row in eps_trend.iterrows():
        current = _num(row.get("current"))
        if current is None:
            continue

        period_out: dict[str, Any] = {"current": current}
        for col in _TREND_COLUMNS[1:]:
            past = _num(row.get(col))
            if past is None or past == 0:
                continue
            change_pct = (current - past) / abs(past) * 100.0
            period_out[col] = round(change_pct, 2)
            if col == "30daysAgo":
                overall.append(change_pct)

        out["by_period"][str(period)] = period_out

    if eps_revisions is not None and not eps_revisions.empty:
        up = down = 0
        for _, row in eps_revisions.iterrows():
            up += _int(row.get("upLast30days")) or 0
            down += _int(row.get("downLast30days")) or 0
        out["analyst_count_30d"] = {"up": up, "down": down}
        if up or down:
            out["net_revision_breadth"] = round((up - down) / (up + down), 3)

    if overall:
        mean = sum(overall) / len(overall)
        out["momentum_30d_pct"] = round(mean, 2)
        out["direction"] = ("raising" if mean > 1.0
                            else "cutting" if mean < -1.0
                            else "stable")

    return out


def _earnings_history(earnings_dates: Optional[pd.DataFrame]) -> tuple[list, Optional[dict]]:
    """Past surprises and the next scheduled result date."""
    if earnings_dates is None or earnings_dates.empty:
        return [], None

    df = earnings_dates.copy()
    df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    df = df[df.index.notna()].sort_index()
    now = pd.Timestamp.now(tz=timezone.utc)

    surprises = []
    for dt, row in df[df.index <= now].tail(8).iloc[::-1].iterrows():
        reported, estimate = _num(row.get("Reported EPS")), _num(row.get("EPS Estimate"))
        if reported is None or estimate is None:
            continue
        surprises.append({
            "date": dt.date().isoformat(),
            "estimate": estimate,
            "reported": reported,
            "surprise_pct": round((reported - estimate) / abs(estimate) * 100, 2)
            if estimate else None,
            "beat": reported > estimate,
        })

    upcoming = df[df.index > now]
    next_result = None
    if not upcoming.empty:
        dt = upcoming.index[0]
        next_result = {
            "date": dt.date().isoformat(),
            "days_away": int((dt - now).days),
            "eps_estimate": _num(upcoming.iloc[0].get("EPS Estimate")),
        }

    return surprises, next_result


def _price_target(targets: Any) -> dict:
    if not isinstance(targets, dict) or not targets:
        return {}
    return {
        "current": _num(targets.get("current")),
        "mean": _num(targets.get("mean")),
        "low": _num(targets.get("low")),
        "high": _num(targets.get("high")),
        "analysts": _int(targets.get("numberOfAnalysts")),
    }


def _summarise(result: dict) -> str:
    bits = []
    rev = result.get("revisions") or {}
    if rev.get("direction") in ("raising", "cutting"):
        bits.append(f"analysts are {rev['direction']} estimates "
                    f"({rev.get('momentum_30d_pct'):+.1f}% over 30 days)")
    elif rev.get("direction") == "stable":
        bits.append("estimates are broadly unchanged over 30 days")

    surprises = result.get("surprises") or []
    if surprises:
        beats = sum(1 for s in surprises[:4] if s.get("beat"))
        bits.append(f"beat in {beats} of the last {min(4, len(surprises))} quarters")

    nxt = result.get("next_result")
    if nxt:
        bits.append(f"next result in {nxt['days_away']} days ({nxt['date']})")

    return "; ".join(bits).capitalize() + "." if bits else "No estimate data available."


def _yf_symbol(trading_symbol: str) -> str:
    base = str(trading_symbol).strip().upper()
    for suffix in ("-EQ", "-BE"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base if base.endswith(".NS") else f"{base}.NS"


def _num(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, 4)


def _int(val) -> Optional[int]:
    f = _num(val)
    return int(f) if f is not None else None
