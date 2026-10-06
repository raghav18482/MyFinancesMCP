"""Delivery and turnover features.

Delivery percentage — the share of traded volume actually settled rather than
squared off intraday — is a conviction proxy. A price move on 70 % delivery is
a different event from the same move on 20 %, and Angel's candle API cannot
tell them apart because it does not carry the field. The NSE bhavcopy does,
which is the main reason the market store is built from it.

Everything here is a z-score or a rank rather than a level, because absolute
turnover is mostly a proxy for market cap and the model already learns that.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from services.features.contract import FLOW

logger = logging.getLogger(__name__)


def build(panel: pd.DataFrame) -> pd.DataFrame:
    """Add delivery and turnover features. Null-safe when the columns are absent."""
    if panel.empty:
        return panel.assign(**{f: np.nan for f in FLOW})

    out = panel.sort_values(["symbol", "date"]).copy()

    if "deliv_pct" not in out.columns:
        logger.info("flow: no delivery column in panel; flow block will be null")
        for f in FLOW:
            out[f] = np.nan
        return out

    deliv = pd.to_numeric(out["deliv_pct"], errors="coerce")
    out["deliv_pct"] = deliv
    out["deliv_pct_z20"] = _rolling_z(out, deliv, 20)
    out["deliv_pct_chg_5"] = deliv - out.groupby("symbol")["deliv_pct"].shift(5)

    turnover = pd.to_numeric(out.get("turnover", pd.Series(np.nan, index=out.index)),
                             errors="coerce")
    # log1p keeps the zero-turnover rows finite instead of producing -inf.
    out["turnover_log"] = np.log1p(turnover.clip(lower=0))
    out["turnover_rank_uni"] = (
        out.assign(_t=turnover).groupby("date")["_t"].rank(pct=True, method="average")
    )

    trades = pd.to_numeric(out.get("trades", pd.Series(np.nan, index=out.index)),
                           errors="coerce")
    avg_trade_size = turnover / trades.replace(0, np.nan)
    out["avg_trade_size_z20"] = _rolling_z(out, avg_trade_size, 20)

    return out


def _rolling_z(panel: pd.DataFrame, series: pd.Series, window: int) -> pd.Series:
    """Per-symbol rolling z-score. Uses only past values — no centring."""
    grouped = series.groupby(panel["symbol"])
    mean = grouped.transform(lambda s: s.rolling(window, min_periods=max(5, window // 2)).mean())
    std = grouped.transform(lambda s: s.rolling(window, min_periods=max(5, window // 2)).std())
    return (series - mean) / std.replace(0, np.nan)


def liquidity_profile(
    panel: pd.DataFrame,
    symbol: str,
    *,
    window: int = 20,
) -> dict:
    """ADV and days-to-exit inputs for one symbol, from the same stored data.

    Used by the research layer rather than the model: average daily value
    traded over ``window`` sessions, plus the delivery level that says how much
    of it is real.
    """
    sym = str(symbol).strip().upper()
    rows = panel[panel["symbol"] == sym].sort_values("date").tail(window)
    if rows.empty:
        return {"symbol": sym, "adv_value": None, "adv_volume": None,
                "deliv_pct_avg": None, "sessions": 0}

    return {
        "symbol": sym,
        "adv_value": float(pd.to_numeric(rows["turnover"], errors="coerce").mean()),
        "adv_volume": float(pd.to_numeric(rows["volume"], errors="coerce").mean()),
        "deliv_pct_avg": float(pd.to_numeric(rows.get("deliv_pct"), errors="coerce").mean())
        if "deliv_pct" in rows.columns else None,
        "sessions": int(len(rows)),
        "as_of": pd.Timestamp(rows["date"].iloc[-1]).date().isoformat(),
    }
