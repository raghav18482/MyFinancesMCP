"""Technical features, computed across the whole panel at once.

The previous implementation called ``extract_features`` once per training row,
recomputing every indicator over the full history each time — roughly 110,000
full recomputes for 20 symbols. Here each indicator is computed once per symbol
as a vector, which turns training from an overnight job into a coffee break and
makes it cheap enough to actually iterate on.

Indicators are computed with the same ``ta`` library and the same windows the
serving path already used, so the numbers are comparable to what the dashboard
shows.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import ADXIndicator, EMAIndicator, MACD, SMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

from services.features.contract import TECHNICAL

logger = logging.getLogger(__name__)

# Longest lookback any feature needs. Rows before this per symbol are warm-up
# and carry nulls; the training pipeline drops them.
MIN_HISTORY = 200


def build(panel: pd.DataFrame) -> pd.DataFrame:
    """Add the technical block to a tidy symbol x date frame.

    ``panel`` must carry symbol, date, open, high, low, close, volume and be
    sorted ascending by date within symbol. Returns a copy.
    """
    if panel.empty:
        return panel.assign(**{f: np.nan for f in TECHNICAL})

    out = panel.sort_values(["symbol", "date"]).copy()
    frames = [_for_symbol(g) for _, g in out.groupby("symbol", sort=False)]
    return pd.concat(frames, ignore_index=True)


def _for_symbol(g: pd.DataFrame) -> pd.DataFrame:
    g = g.copy()
    close, high, low = g["close"], g["high"], g["low"]
    volume = g["volume"].astype(float)
    n = len(g)

    if n < 30:
        # Too short for even the shortest window; emit the columns as nulls so
        # the schema stays stable and the row gets dropped downstream.
        for f in TECHNICAL:
            g[f] = np.nan
        return g

    rsi = RSIIndicator(close, window=14).rsi()
    g["rsi_14"] = rsi
    g["rsi_slope_5"] = rsi - rsi.shift(5)

    macd_ind = MACD(close)
    hist = macd_ind.macd_diff()
    g["macd"] = macd_ind.macd()
    g["macd_signal"] = macd_ind.macd_signal()
    g["macd_histogram"] = hist
    g["macd_slope_5"] = hist - hist.shift(5)

    # Distances are expressed as a fraction of price so they are comparable
    # across a Rs 50 stock and a Rs 5,000 one.
    for window, name in ((20, "sma_20_dist"), (50, "sma_50_dist"), (200, "sma_200_dist")):
        if n >= window:
            sma = SMAIndicator(close, window=window).sma_indicator()
            g[name] = (close - sma) / close.replace(0, np.nan)
        else:
            g[name] = np.nan

    for window, name in ((9, "ema_9_dist"), (21, "ema_21_dist")):
        ema = EMAIndicator(close, window=window).ema_indicator()
        g[name] = (close - ema) / close.replace(0, np.nan)

    bb = BollingerBands(close, window=20)
    upper, lower = bb.bollinger_hband(), bb.bollinger_lband()
    width = (upper - lower)
    g["bb_position"] = (close - lower) / width.replace(0, np.nan)
    g["bb_width"] = width / close.replace(0, np.nan)

    g["adx_14"] = ADXIndicator(high, low, close, window=14).adx()

    stoch = StochasticOscillator(high, low, close, window=14, smooth_window=3)
    g["stoch_k"] = stoch.stoch()
    g["stoch_d"] = stoch.stoch_signal()

    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    g["atr_14_pct"] = atr / close.replace(0, np.nan)

    vol_sma20 = volume.rolling(20, min_periods=5).mean()
    g["volume_ratio"] = volume / vol_sma20.replace(0, np.nan)
    g["volume_trend"] = (
        volume.rolling(5, min_periods=2).mean() / vol_sma20.replace(0, np.nan)
    )

    for k in (1, 3, 5, 10, 20):
        g[f"return_{k}"] = close.pct_change(k)

    daily_ret = close.pct_change()
    for k in (5, 10, 20):
        g[f"volatility_{k}"] = daily_ret.rolling(k, min_periods=max(2, k // 2)).std()

    hl_range = (high - low).replace(0, np.nan)
    body_top = g[["open", "close"]].max(axis=1)
    body_bottom = g[["open", "close"]].min(axis=1)
    g["candle_body_ratio"] = (g["close"] - g["open"]).abs() / hl_range
    g["upper_shadow"] = (high - body_top) / hl_range
    g["lower_shadow"] = (body_bottom - low) / hl_range
    g["high_low_range"] = hl_range / close.replace(0, np.nan)

    # Day of week as a cycle. The hour features from the old intraday contract
    # are gone: on daily bars they were constant, contributing nothing but a
    # false sense of coverage.
    dow = pd.to_datetime(g["date"]).dt.dayofweek
    g["dow_sin"] = np.sin(2 * np.pi * dow / 5)
    g["dow_cos"] = np.cos(2 * np.pi * dow / 5)

    return g


def warmup_mask(panel: pd.DataFrame, min_history: int = MIN_HISTORY) -> pd.Series:
    """True for rows with enough history behind them to trust every feature."""
    return panel.groupby("symbol").cumcount() >= min_history
