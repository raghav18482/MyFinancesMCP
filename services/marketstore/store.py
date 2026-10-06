"""Parquet store for daily bars, partitioned by year.

Layout::

    data/market/daily/year=2026/part.parquet

One file per year keeps the file count low enough to list cheaply while
staying small enough to rewrite when a day is appended. Writes are idempotent:
appending a day that is already stored replaces it rather than duplicating it,
so an interrupted backfill can simply be re-run.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Iterable, Optional

import pandas as pd

from services.marketstore.bhavcopy import SCHEMA

logger = logging.getLogger(__name__)

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DAILY_ROOT = os.path.join(_repo_root, "data", "market", "daily")

_YEAR_DIR = re.compile(r"^year=(\d{4})$")


def _partition_path(year: int, root: str = DAILY_ROOT) -> str:
    return os.path.join(root, f"year={year}", "part.parquet")


def _read_partition(year: int, root: str = DAILY_ROOT) -> pd.DataFrame:
    path = _partition_path(year, root)
    if not os.path.exists(path):
        return pd.DataFrame(columns=SCHEMA)
    return pd.read_parquet(path)


def write_day(df: pd.DataFrame, root: str = DAILY_ROOT) -> int:
    """Write one day's normalised rows. Replaces that day if already present.

    Returns the number of rows written.
    """
    if df is None or df.empty:
        return 0

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    written = 0

    for year, chunk in df.groupby(df["date"].dt.year):
        path = _partition_path(int(year), root)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        existing = _read_partition(int(year), root)
        if not existing.empty:
            existing["date"] = pd.to_datetime(existing["date"])
            days = set(chunk["date"].unique())
            existing = existing[~existing["date"].isin(days)]

        merged = pd.concat([existing, chunk[SCHEMA]], ignore_index=True)
        merged = merged.sort_values(["date", "symbol"]).reset_index(drop=True)
        merged.to_parquet(path, index=False)
        written += len(chunk)

    return written


def stored_years(root: str = DAILY_ROOT) -> list[int]:
    if not os.path.isdir(root):
        return []
    years = []
    for name in os.listdir(root):
        m = _YEAR_DIR.match(name)
        if m and os.path.exists(os.path.join(root, name, "part.parquet")):
            years.append(int(m.group(1)))
    return sorted(years)


def stored_dates(root: str = DAILY_ROOT) -> set[date]:
    """Every trading day already in the store. Used to make backfill resumable."""
    out: set[date] = set()
    for year in stored_years(root):
        df = _read_partition(year, root)
        if df.empty:
            continue
        out.update(pd.to_datetime(df["date"]).dt.date.unique().tolist())
    return out


def last_stored_date(root: str = DAILY_ROOT) -> Optional[date]:
    days = stored_dates(root)
    return max(days) if days else None


def read_range(
    symbols: Optional[Iterable[str]] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    root: str = DAILY_ROOT,
) -> pd.DataFrame:
    """Read a tidy symbol x date frame, filtered by symbol and date range.

    Only the year partitions overlapping the range are read, so a one-year
    query does not pay for eight years of history.
    """
    years = stored_years(root)
    if start is not None:
        years = [y for y in years if y >= start.year]
    if end is not None:
        years = [y for y in years if y <= end.year]
    if not years:
        return pd.DataFrame(columns=SCHEMA)

    wanted = {str(s).strip().upper() for s in symbols} if symbols is not None else None

    frames = []
    for year in years:
        df = _read_partition(year, root)
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        if wanted is not None:
            df = df[df["symbol"].isin(wanted)]
        if start is not None:
            df = df[df["date"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["date"] <= pd.Timestamp(end)]
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame(columns=SCHEMA)

    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["symbol", "date"]).reset_index(drop=True)


def store_summary(root: str = DAILY_ROOT) -> dict:
    """Cheap description of what is on disk, for the backfill CLI and tests."""
    days = stored_dates(root)
    years = stored_years(root)
    rows = 0
    symbols: set[str] = set()
    for year in years:
        df = _read_partition(year, root)
        rows += len(df)
        if not df.empty:
            symbols.update(df["symbol"].unique().tolist())
    return {
        "root": root,
        "years": years,
        "trading_days": len(days),
        "first_day": min(days).isoformat() if days else None,
        "last_day": max(days).isoformat() if days else None,
        "rows": rows,
        "symbols": len(symbols),
    }
