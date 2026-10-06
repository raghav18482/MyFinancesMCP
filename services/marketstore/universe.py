"""The traded universe: NIFTY 50 + Midcap 150 + Smallcap 250, with cap band
and NSE's own industry classification.

Three nselib calls give symbol, company name and industry for 451 names. The
cap band comes from which list a symbol appears in, which is the same basis
AMFI uses for scheme-category eligibility (ranks 1-100 large, 101-250 mid,
251+ small) — close enough to gate on, and the only free source of it.

The industry column is a better peer-group key than ``data/sector_map.json``:
that file is hand-maintained and covers 162 symbols, where this covers all 451
and is maintained by the exchange.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UNIVERSE_PATH = os.path.join(_repo_root, "data", "universe.parquet")

# Which nselib list maps to which band. Order matters: a symbol appearing in
# more than one list (NSE does occasionally overlap around rebalances) takes
# the first, larger band.
CAP_BANDS: tuple[tuple[str, str], ...] = (
    ("large", "nifty50_equity_list"),
    ("mid", "niftymidcap150_equity_list"),
    ("small", "niftysmallcap250_equity_list"),
)

COLUMNS = ["symbol", "company_name", "industry", "cap_band", "built_on"]


def build_universe() -> pd.DataFrame:
    """Fetch the three index lists from NSE and merge them into one frame.

    Network call. Raises if every list fails; a partial result is returned with
    a warning, because one missing band is better than no universe at all.
    """
    from nselib import capital_market

    frames: list[pd.DataFrame] = []
    for band, fn_name in CAP_BANDS:
        try:
            raw = getattr(capital_market, fn_name)()
        except Exception as e:
            logger.warning("universe: %s failed (%s) — band '%s' will be missing",
                           fn_name, e, band)
            continue
        if raw is None or raw.empty:
            logger.warning("universe: %s returned no rows", fn_name)
            continue

        df = pd.DataFrame({
            "symbol": raw["Symbol"].astype(str).str.strip().str.upper(),
            "company_name": raw["Company Name"].astype(str).str.strip(),
            "industry": raw["Industry"].astype(str).str.strip(),
        })
        df["cap_band"] = band
        frames.append(df)

    if not frames:
        raise RuntimeError("universe: all three NSE index lists failed")

    merged = pd.concat(frames, ignore_index=True)
    # First occurrence wins, and CAP_BANDS is ordered large -> small.
    merged = merged.drop_duplicates(subset="symbol", keep="first").reset_index(drop=True)
    merged["built_on"] = date.today().isoformat()
    return merged[COLUMNS]


def save_universe(df: pd.DataFrame, path: str = UNIVERSE_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_parquet(path, index=False)
    logger.info("universe: wrote %d symbols to %s", len(df), path)
    return path


def load_universe(
    path: str = UNIVERSE_PATH,
    *,
    refresh: bool = False,
    max_age_days: int = 45,
) -> pd.DataFrame:
    """Return the cached universe, rebuilding it if missing or stale.

    NSE rebalances its indices half-yearly, so a 45-day default keeps the cache
    useful without going stale across a rebalance.
    """
    if not refresh and os.path.exists(path):
        try:
            df = pd.read_parquet(path)
            built = df["built_on"].iloc[0] if len(df) else None
            if built:
                age = (date.today() - datetime.fromisoformat(str(built)).date()).days
                if age <= max_age_days:
                    return df
                logger.info("universe: cache is %d days old, rebuilding", age)
        except Exception as e:
            logger.warning("universe: cache unreadable (%s), rebuilding", e)

    df = build_universe()
    save_universe(df, path)
    return df


def universe_symbols(path: str = UNIVERSE_PATH, **kw) -> list[str]:
    return load_universe(path, **kw)["symbol"].tolist()


def cap_band_of(symbol: str, universe: Optional[pd.DataFrame] = None) -> Optional[str]:
    """Cap band for one symbol, or None if it is outside the universe."""
    df = universe if universe is not None else load_universe()
    hit = df.loc[df["symbol"] == _norm(symbol), "cap_band"]
    return str(hit.iloc[0]) if len(hit) else None


def industry_of(symbol: str, universe: Optional[pd.DataFrame] = None) -> Optional[str]:
    df = universe if universe is not None else load_universe()
    hit = df.loc[df["symbol"] == _norm(symbol), "industry"]
    return str(hit.iloc[0]) if len(hit) else None


def peers_of(symbol: str, universe: Optional[pd.DataFrame] = None) -> list[str]:
    """Every other symbol in the same NSE industry. Empty if unknown."""
    df = universe if universe is not None else load_universe()
    sym = _norm(symbol)
    industry = industry_of(sym, df)
    if not industry:
        return []
    return df.loc[
        (df["industry"] == industry) & (df["symbol"] != sym), "symbol"
    ].tolist()


def _norm(symbol: str) -> str:
    """Angel uses RELIANCE-EQ, NSE lists use RELIANCE."""
    s = str(symbol).strip().upper()
    for suffix in ("-EQ", "-BE", ".NS"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    u = build_universe()
    save_universe(u)
    print(u["cap_band"].value_counts().to_string())
    print(f"\n{len(u)} symbols, {u['industry'].nunique()} industries")
