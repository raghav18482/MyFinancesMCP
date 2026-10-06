"""Cross-sectional ranks: where a stock sits today relative to everything else.

This is the block the design report ranks above any new indicator. An RSI of 65
means something different on a day when the whole market is at 65 than on a day
when the market is at 40. Ranking each feature within the universe on each date
strips out the market-wide component, so the model learns relative strength
instead of re-learning market direction through thirty correlated proxies.

Two rank sets per base feature:
  ``xs_uni_*`` — percentile within the whole universe that day
  ``xs_ind_*`` — percentile within the stock's NSE industry that day

The industry rank is the one that separates "cheap" from "cheap for a bank".
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from services.features.contract import CROSS_SECTIONAL, CROSS_SECTIONAL_BASE

logger = logging.getLogger(__name__)

# Below this many names on a date, a percentile is noise rather than a signal.
MIN_UNIVERSE_PER_DATE = 20
MIN_INDUSTRY_PER_DATE = 5


def build(panel: pd.DataFrame, universe: pd.DataFrame | None = None) -> pd.DataFrame:
    """Add universe and industry percentile ranks.

    ``panel`` must already carry the technical block. ``universe`` supplies the
    industry mapping; when omitted it is loaded from the market store cache.
    """
    if panel.empty:
        return panel.assign(**{f: np.nan for f in CROSS_SECTIONAL})

    out = panel.copy()

    if "industry" not in out.columns:
        out = attach_industry(out, universe)

    for base in CROSS_SECTIONAL_BASE:
        uni_col, ind_col = f"xs_uni_{base}", f"xs_ind_{base}"
        if base not in out.columns:
            out[uni_col] = np.nan
            out[ind_col] = np.nan
            continue

        out[uni_col] = _rank_within(out, base, ["date"], MIN_UNIVERSE_PER_DATE)
        out[ind_col] = _rank_within(out, base, ["date", "industry"], MIN_INDUSTRY_PER_DATE)

    return out


def attach_industry(panel: pd.DataFrame, universe: pd.DataFrame | None = None) -> pd.DataFrame:
    """Join NSE industry and cap band onto the panel."""
    if universe is None:
        try:
            from services.marketstore.universe import load_universe

            universe = load_universe()
        except Exception as e:
            logger.warning("cross_sectional: universe unavailable (%s); "
                           "industry ranks will be null", e)
            return panel.assign(industry=np.nan, cap_band=np.nan)

    cols = ["symbol", "industry"]
    if "cap_band" in universe.columns:
        cols.append("cap_band")
    return panel.merge(universe[cols], on="symbol", how="left")


def _rank_within(df: pd.DataFrame, column: str, by: list[str], min_members: int) -> pd.Series:
    """Percentile rank of ``column`` within each group, or NaN for thin groups.

    ``pct=True`` already yields 0-1. Groups smaller than ``min_members`` are
    blanked rather than ranked, because a percentile over four names mostly
    encodes which four names happened to list.
    """
    if any(k not in df.columns for k in by):
        return pd.Series(np.nan, index=df.index)

    grouped = df.groupby(by, dropna=True)[column]
    ranks = grouped.rank(pct=True, method="average")
    sizes = grouped.transform("size")
    return ranks.where(sizes >= min_members)
