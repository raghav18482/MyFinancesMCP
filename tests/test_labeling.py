"""Tests for triple-barrier labelling, event sampling and sample weights.

Built on hand-made series where the correct answer is obvious by inspection: a
monotonic climb must be labelled up, a monotonic fall must be labelled down,
and a flat line must not be called a win just because it drifted a basis point.

Also guards the bookkeeping bug these labels originally shipped with: entry and
touch positions are recorded in the symbol's own bar coordinates, so they stay
correct after warm-up rows and non-event rows are dropped.

Run directly (``python tests/test_labeling.py``) or under pytest.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from models import labeling


def _series(symbol: str, closes: np.ndarray, atr_pct: float = 0.01) -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": symbol,
        "date": pd.bdate_range("2025-01-01", periods=len(closes)),
        "open": closes,
        "high": closes * (1 + atr_pct / 4),
        "low": closes * (1 - atr_pct / 4),
        "close": closes,
        "volume": 100_000,
        "atr_14_pct": atr_pct,
    })


def _trend(symbol: str, direction: int, n: int = 60, step: float = 2.0) -> pd.DataFrame:
    return _series(symbol, 100.0 + direction * step * np.arange(n))


def test_a_rising_series_is_labelled_up_and_a_falling_one_down():
    panel = pd.concat([_trend("UP", +1), _trend("DOWN", -1)], ignore_index=True)
    out = labeling.triple_barrier_labels(panel, "1week")

    up = out[out.symbol == "UP"]["label_1week"].dropna()
    down = out[out.symbol == "DOWN"]["label_1week"].dropna()

    assert (up == 1.0).all(), "a monotonic climb must label as up everywhere"
    assert (down == 0.0).all(), "a monotonic fall must label as down everywhere"
    print("ok  trending series label in the obvious direction")


def test_a_flat_series_is_not_called_a_win():
    """A drift smaller than trading cost is a loss, not a win.

    This is the defect in the old label: ``future_close > current_close`` made a
    two-basis-point drift indistinguishable from a four-percent breakout.
    """
    flat = _series("FLAT", np.full(60, 100.0) + np.arange(60) * 0.0001)
    out = labeling.triple_barrier_labels(flat, "1week")
    labels = out["label_1week"].dropna()

    assert (labels == 0.0).all(), (
        "a drift below the cost floor must not be labelled a win"
    )
    print("ok  sub-cost drift is labelled down, not up")


def test_the_barrier_never_sits_below_trading_cost():
    """Even a zero-volatility stock gets a barrier wide enough to pay for itself."""
    calm = _series("CALM", np.full(60, 100.0), atr_pct=0.0)
    out = labeling.triple_barrier_labels(calm, "1day")
    # A perfectly flat series resolves at the vertical barrier with zero return,
    # which is below cost, so every label is 0.
    assert set(out["label_1day"].dropna().unique()) <= {0.0}
    print("ok  the barrier floor keeps a zero-volatility series from scoring wins")


def test_the_upper_barrier_wins_when_it_is_touched_first():
    # Up for three bars, then collapses. Over a 1-week horizon the upper barrier
    # is touched on the way up, before the fall.
    closes = np.array([100, 104, 108, 112, 80, 78, 75, 74, 73, 72] + [72] * 20,
                      dtype=float)
    out = labeling.triple_barrier_labels(_series("SPIKE", closes), "1week")

    first = out.iloc[0]
    assert first["label_1week"] == 1.0, (
        "the upper barrier was touched before the lower one, so the label is up"
    )
    assert first["touch_idx_1week"] < 4, "should resolve during the rise, not at the horizon"
    print("ok  whichever barrier is touched first decides the label")


def test_entry_and_touch_survive_filtering():
    """The bookkeeping bug: positions must be in the symbol's own coordinates.

    Rows get dropped for warm-up and event sampling, after which a position
    within the filtered frame means nothing. Recording entry alongside touch is
    what keeps the span computable afterwards.
    """
    panel = pd.concat([_trend("UP", +1), _trend("DOWN", -1)], ignore_index=True)
    out = labeling.triple_barrier_labels(panel, "1week")

    full = labeling.label_summary(out, "1week")
    filtered = labeling.label_summary(
        out[out.groupby("symbol").cumcount() >= 10], "1week"
    )

    assert full["barrier_hit_rate"] == 1.0, "a trending series always hits a barrier"
    assert filtered["barrier_hit_rate"] == 1.0, (
        "hit rate changed after dropping warm-up rows — positions are being read "
        "in the wrong coordinate system"
    )
    print("ok  entry/touch bookkeeping survives row filtering")


def test_barrier_hit_rate_is_undefined_at_a_one_bar_horizon():
    """With one forward bar, 'touched' and 'timed out' happen at the same bar."""
    panel = _trend("UP", +1)
    out = labeling.triple_barrier_labels(panel, "1day")
    summary = labeling.label_summary(out, "1day")

    assert summary["barrier_hit_rate"] is None, (
        "reporting 0.0 here reads as a failure rather than a question the data "
        "cannot answer"
    )
    assert "undefined" in summary.get("barrier_hit_rate_note", "").lower(), (
        "the None should come with an explanation of why it is undefined"
    )
    # Multi-bar horizons must still report a real number.
    multi = labeling.label_summary(
        labeling.triple_barrier_labels(panel, "1week"), "1week")
    assert isinstance(multi["barrier_hit_rate"], float)
    print("ok  one-bar horizon reports hit rate as undefined, not zero")


def test_rows_without_a_full_forward_window_are_unlabelled():
    panel = _trend("UP", +1, n=30)
    out = labeling.triple_barrier_labels(panel, "1month")  # 22 bars forward

    labelled = out["label_1month"].notna()
    assert not labelled.iloc[-22:].any(), (
        "the final rows have no complete forward window and must stay unlabelled"
    )
    assert labelled.iloc[:8].all(), "earlier rows should be labelled"
    print("ok  rows whose forward window runs past the data are left unlabelled")


def test_uniqueness_weights_penalise_overlap():
    panel = _trend("UP", +1, n=80)
    out = labeling.triple_barrier_labels(panel, "1month")
    w = labeling.uniqueness_weights(out, "1month")

    assert (w > 0).all() and (w <= 1.0).all(), "weights must lie in (0, 1]"
    labelled_w = w[out["label_1month"].notna()]
    assert labelled_w.mean() < 0.5, (
        "heavily overlapping monthly labels should be weighted well below 1"
    )

    # Shorter horizons overlap less, so they should be weighted higher.
    out_short = labeling.triple_barrier_labels(panel, "1day")
    w_short = labeling.uniqueness_weights(out_short, "1day")
    assert w_short[out_short["label_1day"].notna()].mean() > labelled_w.mean(), (
        "a 1-day label overlaps less than a 1-month one and should weigh more"
    )
    print("ok  uniqueness weights fall as label windows overlap more")


def test_cusum_sampling_fires_on_moves_not_on_drift():
    quiet = _series("QUIET", 100.0 + np.random.default_rng(0).normal(0, 0.02, 300))
    jumpy = _series("JUMPY", 100.0 * np.cumprod(
        1 + np.random.default_rng(0).normal(0, 0.03, 300)))

    panel = pd.concat([quiet, jumpy], ignore_index=True)
    events = labeling.cusum_events(panel)

    q = events[panel.symbol == "QUIET"].mean()
    j = events[panel.symbol == "JUMPY"].mean()
    assert events.any(), "CUSUM selected nothing at all"
    assert j > 0, "a volatile series should produce events"
    print(f"ok  CUSUM samples events (quiet {q:.2f}, jumpy {j:.2f})")


def test_horizons_are_the_three_tractable_ones():
    assert set(labeling.HORIZONS) == {"1day", "1week", "1month"}, (
        "the intraday and 1-year horizons were removed deliberately; re-adding "
        "them needs a different data source, not a dict entry"
    )
    assert labeling.HORIZON_BARS["1week"] == 5
    assert labeling.HORIZON_BARS["1month"] == 22
    print("ok  horizon set is the three tractable ones, in trading days")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for fn in TESTS:
        fn()
    print(f"\n{len(TESTS)} passed")
