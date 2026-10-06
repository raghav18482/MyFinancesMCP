"""Market and industry context.

A stock's probability of rising next week is mostly a question about the market
and its sector; the old feature set could see neither. These features are
derived from the panel itself — an equal-weighted universe return as the market
proxy and per-industry aggregates — rather than fetched, so they are available
for every historical date without a second data source and without a
look-ahead risk from a later-revised index series.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from services.features.contract import MARKET_CONTEXT

logger = logging.getLogger(__name__)


def build(panel: pd.DataFrame) -> pd.DataFrame:
    """Add market-wide and industry-relative context columns."""
    if panel.empty:
        return panel.assign(**{f: np.nan for f in MARKET_CONTEXT})

    out = panel.sort_values(["symbol", "date"]).copy()
    if "return_1" not in out.columns:
        out["return_1"] = out.groupby("symbol")["close"].pct_change()

    market = _market_series(out)
    out = out.merge(market, on="date", how="left")

    # Relative strength: the stock's return minus the market's over the same
    # window. Positive means it outperformed, regardless of direction.
    for k in (5, 20):
        stock = out.get(f"return_{k}")
        if stock is None:
            stock = out.groupby("symbol")["close"].pct_change(k)
        out[f"rel_strength_{k}"] = stock - out[f"mkt_return_{k}"]

    out = _add_industry_context(out)
    return out


def _market_series(panel: pd.DataFrame) -> pd.DataFrame:
    """Equal-weighted universe return per date, plus breadth and volatility.

    Equal-weighted rather than cap-weighted on purpose: the universe already
    spans large, mid and small, and an equal weighting makes breadth and the
    market proxy describe the same thing.
    """
    daily = (
        panel.groupby("date")
        .agg(
            mkt_return_1=("return_1", "mean"),
            mkt_breadth_adv_pct=("return_1", lambda s: float((s > 0).mean())),
        )
        .sort_index()
    )

    # Compound the daily mean return into multi-day windows.
    growth = (1.0 + daily["mkt_return_1"].fillna(0.0))
    for k in (5, 20):
        daily[f"mkt_return_{k}"] = growth.rolling(k, min_periods=max(2, k // 2)).apply(
            np.prod, raw=True
        ) - 1.0

    daily["mkt_volatility_20"] = daily["mkt_return_1"].rolling(20, min_periods=10).std()

    return daily.reset_index()


def _add_industry_context(panel: pd.DataFrame) -> pd.DataFrame:
    """Industry return over 5 and 20 days, and the stock's excess over it."""
    needed = {"industry", "return_1"}
    if not needed.issubset(panel.columns) or panel["industry"].isna().all():
        for col in ("ind_return_5", "ind_return_20", "rel_to_industry_20"):
            panel[col] = np.nan
        return panel

    ind_daily = (
        panel.groupby(["industry", "date"])["return_1"]
        .mean()
        .rename("ind_return_1")
        .reset_index()
        .sort_values(["industry", "date"])
    )

    growth = 1.0 + ind_daily["ind_return_1"].fillna(0.0)
    for k in (5, 20):
        ind_daily[f"ind_return_{k}"] = (
            growth.groupby(ind_daily["industry"])
            .rolling(k, min_periods=max(2, k // 2))
            .apply(np.prod, raw=True)
            .reset_index(level=0, drop=True)
            - 1.0
        )

    out = panel.merge(
        ind_daily[["industry", "date", "ind_return_5", "ind_return_20"]],
        on=["industry", "date"],
        how="left",
    )

    stock_20 = out.get("return_20")
    if stock_20 is None:
        stock_20 = out.groupby("symbol")["close"].pct_change(20)
    out["rel_to_industry_20"] = stock_20 - out["ind_return_20"]
    return out
