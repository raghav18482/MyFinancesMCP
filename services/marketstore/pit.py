"""Point-in-time fundamental snapshots.

The problem this solves: ``services/fundamental_service.get_stock_fundamentals``
returns whatever yfinance reports *today*, including figures that were later
restated. Feeding that into a 2022 backtest means the model trains on numbers
nobody could have known in 2022, which makes the backtest look excellent and
the live model fail. That is look-ahead bias, and it is the single easiest way
to fool yourself.

The honest constraint: yfinance cannot be asked what it would have said in
2022, so history cannot be reconstructed. Point-in-time here is therefore
**forward-only capture** — every fetch the running app makes is persisted with
the date we observed it, and the archive accumulates from today. Historical
rows have no snapshot, and ``read_as_of`` returns ``None`` for them rather than
quietly substituting a current value.

Two timestamps, deliberately distinct:
  * ``observed_at`` — when *we* fetched it. This is what a backtest filters on.
  * ``as_of``       — the period the figures describe (latest financials column).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PIT_ROOT = os.path.join(_repo_root, "data", "pit", "fundamentals")

# Flattened columns captured per snapshot. Kept narrow on purpose: these are
# the fields the scorer and the feature builder actually read.
SNAPSHOT_COLUMNS = [
    "symbol", "observed_at", "as_of",
    "score_total", "score_confidence",
    "pillar_valuation", "pillar_profitability", "pillar_balance_sheet", "pillar_growth",
    "pe_ratio", "pb_ratio", "ev_ebitda", "peg_ratio",
    "roe", "roce", "debt_to_equity", "profit_margin", "operating_margin",
    "revenue_growth", "earnings_growth", "promoter_holding", "market_cap",
    "sector", "industry",
]


def _partition_path(day: date, root: str = PIT_ROOT) -> str:
    return os.path.join(root, f"date={day.isoformat()}", "part.parquet")


def flatten(fundamentals: dict[str, Any], observed_at: Optional[date] = None) -> dict:
    """Project a ``get_stock_fundamentals`` result onto the snapshot schema."""
    observed = observed_at or date.today()
    valuation = fundamentals.get("valuation") or {}
    health = fundamentals.get("health") or {}
    score = fundamentals.get("score") or {}
    pillars = {p.get("key"): p for p in (score.get("pillars") or []) if isinstance(p, dict)}

    row = {
        "symbol": _norm(fundamentals.get("symbol", "")),
        "observed_at": pd.Timestamp(observed),
        "as_of": _latest_period(fundamentals),
        "score_total": _num(score.get("total")),
        "score_confidence": _num(score.get("confidence")),
        "pe_ratio": _num(valuation.get("pe_ratio")),
        "pb_ratio": _num(valuation.get("pb_ratio")),
        "ev_ebitda": _num(valuation.get("ev_ebitda")),
        "peg_ratio": _num(valuation.get("peg_ratio")),
        "roe": _num(health.get("roe")),
        "roce": _num(health.get("roce")),
        "debt_to_equity": _num(health.get("debt_to_equity")),
        "profit_margin": _num(health.get("profit_margin")),
        "operating_margin": _num(health.get("operating_margin")),
        "revenue_growth": _num(health.get("revenue_growth")),
        "earnings_growth": _num(health.get("earnings_growth")),
        "promoter_holding": _num(health.get("promoter_holding")),
        "market_cap": _num(fundamentals.get("market_cap")),
        "sector": fundamentals.get("sector") or "",
        "industry": fundamentals.get("industry") or "",
    }
    for key in ("valuation", "profitability", "balance_sheet", "growth"):
        row[f"pillar_{key}"] = _num((pillars.get(key) or {}).get("score"))
    return row


def write_snapshot(
    fundamentals: dict[str, Any],
    *,
    observed_at: Optional[date] = None,
    root: str = PIT_ROOT,
) -> bool:
    """Persist one snapshot. Idempotent per (symbol, day); never raises.

    Called from the hot path of ``get_stock_fundamentals``, so a failure here
    must not break the request that triggered it.
    """
    try:
        row = flatten(fundamentals, observed_at)
        if not row["symbol"]:
            return False
        # A failed fetch carries no information worth preserving.
        if fundamentals.get("error") and row["score_total"] is None:
            return False

        day = (observed_at or date.today())
        path = _partition_path(day, root)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        new = pd.DataFrame([row], columns=SNAPSHOT_COLUMNS)
        if os.path.exists(path):
            existing = pd.read_parquet(path)
            existing = existing[existing["symbol"] != row["symbol"]]
            new = pd.concat([existing, new], ignore_index=True)

        new.to_parquet(path, index=False)
        return True
    except Exception as e:
        logger.debug("pit: snapshot write failed for %s: %s",
                     fundamentals.get("symbol"), e)
        return False


def read_all(root: str = PIT_ROOT) -> pd.DataFrame:
    """Every snapshot ever captured, as one frame."""
    if not os.path.isdir(root):
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    frames = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, "part.parquet")
        if os.path.exists(path):
            try:
                frames.append(pd.read_parquet(path))
            except Exception as e:
                logger.warning("pit: unreadable partition %s (%s)", name, e)

    if not frames:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    df = pd.concat(frames, ignore_index=True)
    df["observed_at"] = pd.to_datetime(df["observed_at"])
    return df.sort_values(["symbol", "observed_at"]).reset_index(drop=True)


def read_as_of(symbol: str, as_of: date, root: str = PIT_ROOT) -> Optional[dict]:
    """The newest snapshot observed on or before ``as_of``, or None.

    Returning None is the whole point. There is deliberately no fallback to the
    current value: a caller that cannot find a snapshot must drop the row, not
    substitute a figure from the future.
    """
    df = read_all(root)
    if df.empty:
        return None
    sym = _norm(symbol)
    hit = df[(df["symbol"] == sym) & (df["observed_at"] <= pd.Timestamp(as_of))]
    if hit.empty:
        return None
    return hit.iloc[-1].to_dict()


def as_of_panel(
    symbols: list[str],
    dates: list[date],
    root: str = PIT_ROOT,
) -> pd.DataFrame:
    """Vectorised ``read_as_of`` over a symbol x date grid.

    Returns one row per (symbol, date) that has a snapshot; pairs with no
    snapshot are simply absent, so a downstream join leaves them null and the
    training pipeline can drop or impute them explicitly.
    """
    snaps = read_all(root)
    if snaps.empty or not symbols or not dates:
        return pd.DataFrame(columns=["symbol", "date"] + _feature_columns())

    wanted = {_norm(s) for s in symbols}
    snaps = snaps[snaps["symbol"].isin(wanted)]
    if snaps.empty:
        return pd.DataFrame(columns=["symbol", "date"] + _feature_columns())

    grid = pd.DataFrame(
        [(s, pd.Timestamp(d)) for s in sorted(wanted) for d in sorted(set(dates))],
        columns=["symbol", "date"],
    ).sort_values(["symbol", "date"])

    # merge_asof is the point-in-time join: for each grid date take the latest
    # snapshot at or before it, per symbol, and nothing after it.
    merged = pd.merge_asof(
        grid,
        snaps.sort_values("observed_at"),
        left_on="date",
        right_on="observed_at",
        by="symbol",
        direction="backward",
    )
    return merged.dropna(subset=["observed_at"]).reset_index(drop=True)


def coverage(root: str = PIT_ROOT) -> dict:
    """How much point-in-time history exists. Reported by the training run."""
    df = read_all(root)
    if df.empty:
        return {"snapshots": 0, "symbols": 0, "first": None, "last": None}
    return {
        "snapshots": len(df),
        "symbols": int(df["symbol"].nunique()),
        "first": df["observed_at"].min().date().isoformat(),
        "last": df["observed_at"].max().date().isoformat(),
    }


def _feature_columns() -> list[str]:
    return [c for c in SNAPSHOT_COLUMNS if c not in ("symbol", "observed_at")]


def _latest_period(fundamentals: dict) -> Optional[pd.Timestamp]:
    """The period the figures describe, taken from the newest revenue year."""
    trend = fundamentals.get("revenue_trend") or []
    years = [str(r.get("year")) for r in trend if isinstance(r, dict) and r.get("year")]
    if not years:
        return None
    try:
        return pd.Timestamp(datetime(int(max(years)), 12, 31))
    except Exception:
        return None


def _num(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # drop NaN


def _norm(symbol: str) -> str:
    s = str(symbol).strip().upper()
    for suffix in ("-EQ", "-BE", ".NS"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s
