"""One trading day of the whole NSE cash market, normalised.

``nselib.capital_market.bhav_copy_with_delivery`` returns ~3,500 rows per day:
OHLCV, turnover, trade count, and crucially delivery quantity and percentage.
Delivery percentage is a conviction proxy that Angel's candle API does not
expose at all, so this source is strictly richer for the same work.

Two gotchas it is easy to lose an afternoon to:
  * the date argument is ``%d-%m-%Y``, not ISO and not ``%d-%b-%Y``
  * the returned column names carry leading/trailing spaces inconsistently,
    and numeric columns arrive as strings with thousands separators
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# The date format nselib's bhav_copy_with_delivery expects.
NSE_DATE_FMT = "%d-%m-%Y"

# Earliest date the delivery-enriched archive serves. Verified: 2019 dates
# return "Data not found", 02-01-2020 onward works.
ARCHIVE_FLOOR = date(2020, 1, 1)

# Normalised schema written to the store. Keep this stable: the Parquet
# partitions are appended to, not rewritten.
SCHEMA = [
    "symbol", "date", "open", "high", "low", "close", "prev_close",
    "vwap", "volume", "turnover", "trades", "deliv_qty", "deliv_pct",
]

# Source column -> normalised name. Source names are matched after stripping
# whitespace and upper-casing.
_COLUMN_MAP = {
    "SYMBOL": "symbol",
    "DATE1": "date",
    "OPEN_PRICE": "open",
    "HIGH_PRICE": "high",
    "LOW_PRICE": "low",
    "CLOSE_PRICE": "close",
    "PREV_CLOSE": "prev_close",
    "AVG_PRICE": "vwap",
    "TTL_TRD_QNTY": "volume",
    "TURNOVER_LACS": "turnover",
    "NO_OF_TRADES": "trades",
    "DELIV_QTY": "deliv_qty",
    "DELIV_PER": "deliv_pct",
}

_NUMERIC = [c for c in SCHEMA if c not in ("symbol", "date")]


class NoDataForDate(Exception):
    """NSE has no bhavcopy for this date — a holiday, a weekend, or pre-archive."""


def fetch_day(day: date) -> pd.DataFrame:
    """Fetch and normalise one trading day. Raises NoDataForDate on a non-trading day."""
    from nselib import capital_market

    if day < ARCHIVE_FLOOR:
        raise NoDataForDate(f"{day} is before the archive floor {ARCHIVE_FLOOR}")

    try:
        raw = capital_market.bhav_copy_with_delivery(day.strftime(NSE_DATE_FMT))
    except Exception as e:
        # nselib raises FileNotFoundError for holidays and weekends; it is not
        # worth distinguishing those from a transient failure here, because the
        # backfill treats both as "skip and carry on".
        raise NoDataForDate(f"{day}: {e}") from e

    if raw is None or raw.empty:
        raise NoDataForDate(f"{day}: empty frame")

    return normalise(raw, day)


def normalise(raw: pd.DataFrame, day: Optional[date] = None) -> pd.DataFrame:
    """Turn a raw bhavcopy frame into the stored schema.

    Split out from ``fetch_day`` so it can be tested against a saved fixture
    without touching the network.
    """
    df = raw.copy()
    df.columns = [str(c).strip().upper() for c in df.columns]

    missing = set(_COLUMN_MAP) - set(df.columns)
    if missing:
        raise ValueError(f"bhavcopy missing expected columns: {sorted(missing)}")

    # Cash equity only. SME, bonds and ETFs ride in the same file under other
    # series codes and would pollute cross-sectional ranks.
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.strip().str.upper() == "EQ"]

    out = pd.DataFrame({new: df[old] for old, new in _COLUMN_MAP.items()})
    out["symbol"] = out["symbol"].astype(str).str.strip().str.upper()

    if day is not None:
        out["date"] = pd.Timestamp(day)
    else:
        out["date"] = pd.to_datetime(out["date"].astype(str).str.strip(),
                                     format="%d-%b-%Y", errors="coerce")

    for col in _NUMERIC:
        out[col] = _to_numeric(out[col])

    # Turnover arrives in lakhs; store rupees so downstream liquidity maths
    # does not have to remember the unit.
    out["turnover"] = out["turnover"] * 100_000.0

    out = out.dropna(subset=["symbol", "date", "close"])
    out = out[out["close"] > 0]
    out = out.drop_duplicates(subset=["symbol", "date"], keep="last")

    return out[SCHEMA].reset_index(drop=True)


def _to_numeric(s: pd.Series) -> pd.Series:
    """Coerce a column that may be numeric, or strings with commas and dashes."""
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce")
    cleaned = (
        s.astype(str)
        .str.strip()
        .str.replace(",", "", regex=False)
        .replace({"-": None, "": None, "nan": None, "None": None})
    )
    return pd.to_numeric(cleaned, errors="coerce")


def parse_day(value: str | date | datetime) -> date:
    """Accept ISO, NSE format, or a date object."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", NSE_DATE_FMT, "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {value!r}")
