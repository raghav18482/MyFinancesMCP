"""Triple-barrier labelling, event sampling and sample weights.

The previous label was ``1 if future_close > current_close else 0``. That gives
a +0.02 % drift and a +4 % breakout the same label, so most of the training
signal sat in moves smaller than the bid-ask spread plus brokerage plus STT —
moves that could never have been traded profitably.

Triple barriers fix three things at once:

  * **volatility awareness** — barriers are placed at a multiple of ATR, so a
    1 % move in a quiet stock and a 3 % move in a volatile one are comparable
  * **a real trade** — an upper barrier is a target, a lower one is a stop, and
    the vertical one is the holding period; the label encodes a decision
  * **cost awareness** — the barrier floor is set wide enough to clear a
    round trip, so the model stops learning untradeable noise

Reference: the triple-barrier and sample-uniqueness methods are standard
financial-ML practice (Lopez de Prado, *Advances in Financial Machine
Learning*); this is a straightforward implementation of them.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Horizons in trading days. Intraday horizons are gone: they are not tractable
# from daily bars, and the old 1day/1week/1month/1year entries were daily-bar
# conventions applied to five-minute data.
HORIZON_BARS: dict[str, int] = {
    "1day": 1,
    "1week": 5,
    "1month": 22,
}
HORIZONS = tuple(HORIZON_BARS)

# Round-trip cost on NSE delivery, as a fraction of notional. Brokerage plus
# STT plus exchange charges plus GST plus stamp duty, rounded up, with a
# slippage allowance. Deliberately conservative — a barrier that does not clear
# cost teaches the model to chase noise.
ROUND_TRIP_COST = 0.0035  # 35 bps

# Barrier half-width as a multiple of ATR. 1.0 keeps most events resolving at a
# horizontal barrier rather than timing out.
DEFAULT_ATR_MULT = 1.0


def triple_barrier_labels(
    panel: pd.DataFrame,
    horizon: str,
    *,
    atr_mult: float = DEFAULT_ATR_MULT,
    cost: float = ROUND_TRIP_COST,
) -> pd.DataFrame:
    """Label every row by which barrier its forward path touches first.

    Returns the panel with, per horizon:
      ``label_<h>``      1 if the upper barrier is hit first, 0 if the lower is
      ``ret_<h>``        realised return to the touch (or to the horizon)
      ``entry_idx_<h>``  the row's own position within its symbol's series
      ``touch_idx_<h>``  position at which the label resolved

    Entry and touch are both recorded in the symbol's own coordinates, and both
    survive filtering. Storing only the touch would be enough while every row is
    present, but rows get dropped for warm-up and event sampling, after which a
    positional index no longer lines up with anything.

    Rows whose forward window runs past the end of the data are dropped: a
    label that peeks at data we do not have is worse than no label.
    """
    if horizon not in HORIZON_BARS:
        raise ValueError(f"unknown horizon {horizon!r}; expected one of {HORIZONS}")

    bars = HORIZON_BARS[horizon]
    frames = [
        _label_symbol(g, bars, horizon, atr_mult, cost)
        for _, g in panel.sort_values(["symbol", "date"]).groupby("symbol", sort=False)
    ]
    return pd.concat(frames, ignore_index=True)


def _label_symbol(
    g: pd.DataFrame,
    bars: int,
    horizon: str,
    atr_mult: float,
    cost: float,
) -> pd.DataFrame:
    g = g.copy()
    n = len(g)
    label_col = f"label_{horizon}"
    ret_col = f"ret_{horizon}"
    touch_col = f"touch_idx_{horizon}"
    entry_col = f"entry_idx_{horizon}"

    if n <= bars:
        g[label_col] = np.nan
        g[ret_col] = np.nan
        g[touch_col] = np.nan
        g[entry_col] = np.nan
        return g

    close = g["close"].to_numpy(dtype="float64")
    high = g["high"].to_numpy(dtype="float64")
    low = g["low"].to_numpy(dtype="float64")

    # Barrier width: ATR-scaled, floored so it always clears a round trip.
    if "atr_14_pct" in g.columns:
        atr_pct = pd.to_numeric(g["atr_14_pct"], errors="coerce").to_numpy(dtype="float64")
    else:
        atr_pct = np.full(n, np.nan)
    atr_pct = np.where(np.isfinite(atr_pct), atr_pct, 0.0)

    # The horizon scales the barrier: a one-month hold should not use the same
    # width as a one-day hold, or almost everything times out.
    width = np.maximum(atr_pct * atr_mult * np.sqrt(bars), cost * 1.5)

    labels = np.full(n, np.nan)
    rets = np.full(n, np.nan)
    touches = np.full(n, np.nan)
    entries = np.full(n, np.nan)

    for i in range(n - bars):
        entry = close[i]
        if not np.isfinite(entry) or entry <= 0:
            continue

        upper = entry * (1.0 + width[i])
        lower = entry * (1.0 - width[i])
        stop = i + bars

        hit = 0
        touch_at = stop
        for j in range(i + 1, stop + 1):
            # A bar that straddles both barriers is ambiguous at daily
            # resolution; treat it as a stop-out, which is the conservative
            # reading for a long signal.
            if low[j] <= lower:
                hit = -1
                touch_at = j
                break
            if high[j] >= upper:
                hit = 1
                touch_at = j
                break

        exit_price = close[touch_at]
        realised = (exit_price / entry) - 1.0

        if hit == 1:
            labels[i] = 1.0
        elif hit == -1:
            labels[i] = 0.0
        else:
            # Timed out at the vertical barrier. Fall back to the sign of the
            # realised move net of cost, so a flat drift is not called a win.
            labels[i] = 1.0 if realised > cost else 0.0

        rets[i] = realised
        touches[i] = touch_at
        entries[i] = i

    g[label_col] = labels
    g[ret_col] = rets
    g[touch_col] = touches
    g[entry_col] = entries
    return g


# ── Event sampling ─────────────────────────────────────────────────────────
def cusum_events(
    panel: pd.DataFrame,
    *,
    threshold_mult: float = 1.0,
    vol_window: int = 20,
) -> pd.Series:
    """Boolean mask selecting bars where something actually happened.

    A symmetric CUSUM filter on log returns: a bar is kept when the cumulative
    move since the last event exceeds a volatility-scaled threshold. Sampling
    every bar instead produces rows that are near-duplicates of their
    neighbours, which is what made the old validation optimistic.
    """
    out = pd.Series(False, index=panel.index)

    for _, g in panel.sort_values(["symbol", "date"]).groupby("symbol", sort=False):
        close = pd.to_numeric(g["close"], errors="coerce")
        log_ret = np.log(close).diff()
        vol = log_ret.rolling(vol_window, min_periods=max(5, vol_window // 2)).std()

        s_pos = s_neg = 0.0
        idx = g.index
        lr = log_ret.to_numpy()
        th = (vol * threshold_mult).to_numpy()

        for k in range(len(g)):
            r, t = lr[k], th[k]
            if not np.isfinite(r) or not np.isfinite(t) or t <= 0:
                continue
            s_pos = max(0.0, s_pos + r)
            s_neg = min(0.0, s_neg + r)
            if s_pos > t:
                s_pos = 0.0
                out.loc[idx[k]] = True
            elif s_neg < -t:
                s_neg = 0.0
                out.loc[idx[k]] = True

    return out


# ── Sample weights ─────────────────────────────────────────────────────────
def uniqueness_weights(panel: pd.DataFrame, horizon: str) -> pd.Series:
    """Down-weight rows whose label windows overlap their neighbours'.

    Two rows one bar apart share almost their whole forward window, so they
    carry almost the same information. Counting how many labels span each bar
    and weighting by the inverse stops a cluster of overlapping rows from
    outvoting an isolated one.
    """
    touch_col = f"touch_idx_{horizon}"
    entry_col = f"entry_idx_{horizon}"
    if touch_col not in panel.columns or entry_col not in panel.columns:
        return pd.Series(1.0, index=panel.index)

    weights = pd.Series(np.nan, index=panel.index)

    for _, g in panel.groupby("symbol", sort=False):
        entry = pd.to_numeric(g[entry_col], errors="coerce").to_numpy()
        touch = pd.to_numeric(g[touch_col], errors="coerce").to_numpy()
        valid = np.isfinite(entry) & np.isfinite(touch)
        if not valid.any():
            continue

        # Work in the symbol's own bar coordinates, not in positions within this
        # (possibly filtered) frame — those two stopped agreeing the moment
        # warm-up rows and non-event rows were dropped.
        span_end = int(np.nanmax(touch[valid])) + 1
        concurrency = np.zeros(span_end + 1, dtype="float64")

        for a, b in zip(entry[valid].astype(int), touch[valid].astype(int)):
            concurrency[a:b + 1] += 1.0

        w = np.full(len(g), np.nan)
        for k in np.flatnonzero(valid):
            a, b = int(entry[k]), int(touch[k])
            overlap = concurrency[a:b + 1]
            overlap = overlap[overlap > 0]
            w[k] = float(np.mean(1.0 / overlap)) if len(overlap) else 1.0

        weights.loc[g.index] = w

    return weights.fillna(1.0)


def label_summary(panel: pd.DataFrame, horizon: str) -> dict:
    """Class balance and resolution mix, for the training report."""
    label_col, ret_col = f"label_{horizon}", f"ret_{horizon}"
    touch_col, entry_col = f"touch_idx_{horizon}", f"entry_idx_{horizon}"
    labelled = panel[panel[label_col].notna()]
    if labelled.empty:
        return {"rows": 0}

    out = {
        "rows": int(len(labelled)),
        "positive_rate": round(float(labelled[label_col].mean()), 4),
        "mean_return": round(float(labelled[ret_col].mean()), 5),
        "median_abs_return": round(float(labelled[ret_col].abs().median()), 5),
    }

    # The share of labels that resolved at a horizontal barrier rather than
    # timing out. A hit rate near zero means the barriers are too wide to ever
    # be touched, which quietly turns this back into a fixed-horizon label.
    #
    # Undefined at a one-bar horizon: there is exactly one forward bar, so
    # "touched a barrier" and "ran out of time" happen at the same bar and
    # cannot be told apart. Reporting 0.0 there would read as a failure rather
    # than as a question the data cannot answer.
    bars = HORIZON_BARS[horizon]
    if touch_col in labelled.columns and entry_col in labelled.columns:
        span = pd.to_numeric(labelled[touch_col], errors="coerce") - \
            pd.to_numeric(labelled[entry_col], errors="coerce")
        out["median_bars_held"] = float(span.median())
        out["barrier_hit_rate"] = (
            None if bars <= 1 else round(float((span < bars).mean()), 4)
        )
        if bars <= 1:
            out["barrier_hit_rate_note"] = (
                "undefined at a 1-bar horizon; the label reduces to the signed "
                "move net of cost"
            )

    return out
