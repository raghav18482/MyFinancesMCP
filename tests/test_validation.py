"""Tests for purged, embargoed walk-forward validation.

This is the regression guard for the defect that made the old model's reported
accuracy meaningless: ``train_test_split(..., shuffle=True)`` on rows generated
by a sliding window, where adjacent rows shared 49 of their 50 bars of history
and their label windows overlapped almost completely.

The assertions below encode the three properties that make a split honest:
training always precedes testing in time, no training row's label window
reaches into the test block, and an embargo gap separates them. If someone
later "simplifies" the splitter, these fail.

Run directly (``python tests/test_validation.py``) or under pytest.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from models.validation import (
    WalkForward,
    aggregate,
    calibration_error,
    evaluate,
)


def _dates(n_days: int = 900, symbols: int = 5) -> pd.Series:
    """A realistic panel: several symbols sharing each trading date."""
    days = pd.bdate_range("2022-01-03", periods=n_days)
    rows = [d for d in days for _ in range(symbols)]
    return pd.Series(sorted(rows))


def test_no_training_row_ever_sits_after_its_test_block():
    dates = _dates()
    wf = WalkForward(n_splits=5, test_size_days=60, label_span_days=7, embargo_days=5)

    folds = list(wf.split(dates))
    assert folds, "splitter produced no folds"

    for fold in folds:
        train_dates = dates.iloc[fold.train_idx]
        test_dates = dates.iloc[fold.test_idx]
        assert train_dates.max() < test_dates.min(), (
            f"fold {fold.index}: a training row is dated at or after the test "
            f"block — this is the shuffle bug returning"
        )
    print(f"ok  all {len(folds)} folds keep training strictly before testing")


def test_training_and_test_indices_never_intersect():
    dates = _dates()
    wf = WalkForward(n_splits=5, test_size_days=60, label_span_days=7, embargo_days=5)

    for fold in wf.split(dates):
        overlap = np.intersect1d(fold.train_idx, fold.test_idx)
        assert overlap.size == 0, (
            f"fold {fold.index}: {overlap.size} rows appear in both train and test"
        )
    print("ok  no row appears in both train and test in any fold")


def test_purge_removes_rows_whose_label_reaches_into_the_test_block():
    """A label spanning N days means the last N days of training saw the future."""
    dates = _dates()
    label_span = 21  # a one-month horizon

    wf = WalkForward(n_splits=4, test_size_days=60,
                     label_span_days=label_span, embargo_days=0)

    folds = list(wf.split(dates))
    assert folds, "splitter produced no folds"

    for fold in folds:
        train_dates = dates.iloc[fold.train_idx]
        gap_days = (fold.test_start - train_dates.max()).days
        assert gap_days >= label_span, (
            f"fold {fold.index}: only {gap_days} days between the last training "
            f"row and the test block, but labels span {label_span} days — those "
            f"training rows have seen the test period's outcome"
        )
        assert fold.purged > 0, f"fold {fold.index} reported purging no rows"
    print("ok  purging leaves at least the full label span between train and test")


def test_embargo_widens_the_gap_further():
    dates = _dates()
    common = dict(n_splits=4, test_size_days=60, label_span_days=7)

    without = list(WalkForward(**common, embargo_days=0).split(dates))
    with_embargo = list(WalkForward(**common, embargo_days=15).split(dates))

    assert len(without) == len(with_embargo), "embargo should not change fold count"

    for a, b in zip(without, with_embargo):
        assert len(b.train_idx) < len(a.train_idx), (
            f"fold {a.index}: embargo removed no training rows"
        )
        assert b.embargoed > 0, f"fold {b.index} reported no embargoed rows"

        gap_without = (a.test_start - dates.iloc[a.train_idx].max()).days
        gap_with = (b.test_start - dates.iloc[b.train_idx].max()).days
        assert gap_with > gap_without, "embargo did not widen the gap"
    print("ok  embargo removes additional training rows nearest the test block")


def test_folds_walk_forward_in_time():
    dates = _dates()
    folds = list(WalkForward(n_splits=5, test_size_days=60).split(dates))

    starts = [f.test_start for f in folds]
    assert starts == sorted(starts), "test blocks should advance through time"
    assert len(set(starts)) == len(starts), "test blocks should not repeat"

    # An expanding window: each fold trains on at least as much as the last.
    sizes = [len(f.train_idx) for f in folds]
    assert sizes == sorted(sizes), f"training set should expand, got {sizes}"
    print("ok  folds walk forward with an expanding training window")


def test_short_history_still_yields_folds():
    """A short store should degrade to fewer folds, not silently zero."""
    dates = _dates(n_days=200, symbols=3)
    folds = list(WalkForward(n_splits=3, test_size_days=30,
                             label_span_days=7, min_train_days=250).split(dates))
    assert folds, "a short panel produced no folds at all"
    for fold in folds:
        assert dates.iloc[fold.train_idx].max() < fold.test_start
    print("ok  short history reduces the warm-up rather than yielding nothing")


# ── Metrics ────────────────────────────────────────────────────────────────
def test_auc_is_half_for_a_constant_prediction():
    y = np.array([0, 1] * 50, dtype=float)
    p = np.full(100, 0.7)
    m = evaluate(y, p)
    assert abs(m["auc"] - 0.5) < 1e-9, f"constant prediction should score 0.5, got {m['auc']}"
    print("ok  a constant prediction scores AUC 0.5 exactly")


def test_auc_is_one_for_a_perfect_ranking():
    y = np.array([0] * 50 + [1] * 50, dtype=float)
    p = np.linspace(0.0, 1.0, 100)
    m = evaluate(y, p)
    assert abs(m["auc"] - 1.0) < 1e-9, f"perfect ranking should score 1.0, got {m['auc']}"
    print("ok  a perfect ranking scores AUC 1.0")


def test_ic_detects_a_known_relationship():
    rng = np.random.default_rng(0)
    p = rng.uniform(size=400)
    returns = p * 0.1 + rng.normal(scale=0.01, size=400)   # strongly related
    y = (returns > returns.mean()).astype(float)
    m = evaluate(y, p, returns)
    assert m["ic"] > 0.8, f"IC should be high for a near-deterministic link, got {m['ic']}"

    scrambled = rng.permutation(returns)
    m2 = evaluate((scrambled > scrambled.mean()).astype(float), p, scrambled)
    assert abs(m2["ic"]) < 0.2, f"IC should be near zero for noise, got {m2['ic']}"
    print("ok  IC separates a real relationship from noise")


def test_calibration_error_rewards_honest_probabilities():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.05, 0.95, size=4000)
    honest = (rng.uniform(size=4000) < p).astype(float)       # p means what it says
    overconfident = (rng.uniform(size=4000) < 0.5).astype(float)  # p means nothing

    ece_honest = calibration_error(evaluate(honest, p)["reliability"])
    ece_bad = calibration_error(evaluate(overconfident, p)["reliability"])

    assert ece_honest < 0.05, f"well-calibrated input should score low, got {ece_honest}"
    assert ece_bad > ece_honest, "miscalibrated input should score worse"
    print(f"ok  calibration error separates honest ({ece_honest}) from "
          f"miscalibrated ({ece_bad})")


def test_evaluate_handles_a_single_class_test_block():
    m = evaluate(np.ones(50), np.linspace(0, 1, 50))
    assert m["auc"] is None and "note" in m, "a single-class block should report, not crash"
    print("ok  a single-class test block reports instead of raising")


def test_aggregate_reports_spread_not_just_the_mean():
    folds = [{"auc": 0.49, "n": 100}, {"auc": 0.61, "n": 100}]
    agg = aggregate(folds)
    assert abs(agg["auc"] - 0.55) < 1e-9
    assert agg["auc_std"] > 0.05, "spread across folds must be reported"
    assert agg["total_test_rows"] == 200
    print("ok  aggregate reports fold spread alongside the mean")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for fn in TESTS:
        fn()
    print(f"\n{len(TESTS)} passed")
