"""Tests for the feature contract and the guards built on it.

The contract exists because feature *names* are not a contract. The old
pipeline trained on five-minute bars and served daily bars using an identical
35-name list, so nothing errored and the model silently read a different
distribution at inference time.

The two assertions that matter here are that a version mismatch raises, and
that an interval mismatch raises. Both were impossible to express before.

Run directly (``python tests/test_feature_contract.py``) or under pytest.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from services import features
from services.features import contract


def _panel(symbols=("AAA", "BBB", "CCC"), n: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    frames = []
    for i, sym in enumerate(symbols):
        close = 100.0 * np.cumprod(1 + rng.normal(0.0005, 0.015, n))
        frames.append(pd.DataFrame({
            "symbol": sym,
            "date": pd.bdate_range("2024-01-01", periods=n),
            "open": close * (1 + rng.normal(0, 0.002, n)),
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": rng.integers(1e5, 1e6, n),
            "turnover": close * rng.integers(1e5, 1e6, n),
            "trades": rng.integers(1_000, 50_000, n),
            "deliv_pct": rng.uniform(20, 80, n),
            "industry": f"IND{i % 2}",
        }))
    return pd.concat(frames, ignore_index=True)


# ── The contract itself ────────────────────────────────────────────────────
def test_contract_has_no_duplicate_features():
    assert len(contract.FEATURE_NAMES) == len(set(contract.FEATURE_NAMES)), (
        "a duplicated feature name silently shifts every column after it"
    )
    print(f"ok  contract lists {len(contract.FEATURE_NAMES)} distinct features")


def test_every_feature_belongs_to_exactly_one_block():
    for feature in contract.FEATURE_NAMES:
        owners = [name for name, feats in contract.BLOCKS.items() if feature in feats]
        assert len(owners) == 1, f"{feature} belongs to blocks {owners}"
    print("ok  every feature belongs to exactly one block")


def test_blocks_concatenate_to_the_feature_list_in_order():
    rebuilt = [f for block in contract.BLOCKS.values() for f in block]
    assert rebuilt == contract.FEATURE_NAMES, (
        "the ordered feature list must match the block order — the model takes a "
        "bare array, so order is part of the contract"
    )
    print("ok  block order reproduces the feature list exactly")


# ── The builders honour the contract ───────────────────────────────────────
def test_build_panel_emits_every_contract_feature():
    panel = features.build_panel(_panel())
    missing = contract.validate_frame_columns(panel.columns)
    assert not missing, f"build_panel did not emit {len(missing)} features: {missing[:8]}"
    print("ok  build_panel emits every feature the contract names")


def test_feature_matrix_matches_the_contract_width_and_order():
    panel = features.build_panel(_panel())
    X = features.feature_matrix(panel)

    assert X.shape[1] == len(contract.FEATURE_NAMES), (
        f"matrix has {X.shape[1]} columns, contract names "
        f"{len(contract.FEATURE_NAMES)}"
    )
    assert X.shape[0] == len(panel)

    # Column order must follow the contract, not the frame's own order.
    first = contract.FEATURE_NAMES[0]
    expected = pd.to_numeric(panel[first], errors="coerce").to_numpy(dtype="float64")
    np.testing.assert_allclose(X[:, 0], expected, equal_nan=True)
    print("ok  feature_matrix follows contract order and width")


def test_feature_matrix_converts_infinities_to_missing():
    panel = features.build_panel(_panel())
    panel.loc[panel.index[:3], contract.FEATURE_NAMES[0]] = np.inf
    X = features.feature_matrix(panel)
    assert np.isnan(X[:3, 0]).all(), (
        "an infinity from a divide-by-zero must become missing, not an enormous value"
    )
    print("ok  infinities are converted to missing rather than passed to the model")


def test_feature_matrix_rejects_an_incomplete_frame():
    panel = features.build_panel(_panel()).drop(columns=[contract.FEATURE_NAMES[5]])
    try:
        features.feature_matrix(panel)
    except contract.FeatureContractMismatch as e:
        assert contract.FEATURE_NAMES[5] in str(e)
        print("ok  an incomplete frame raises and names the missing feature")
        return
    raise AssertionError("a frame missing a contract feature must raise")


# ── The guards ─────────────────────────────────────────────────────────────
def test_a_version_mismatch_raises():
    contract.validate_version(contract.FEATURE_SET_VERSION)  # matching: no raise
    try:
        contract.validate_version(contract.FEATURE_SET_VERSION + 1, model_name="stale")
    except contract.FeatureContractMismatch as e:
        assert "stale" in str(e) and "retrain" in str(e).lower()
        print("ok  a feature-set version mismatch raises instead of predicting")
        return
    raise AssertionError("a version mismatch must raise")


def test_an_interval_mismatch_raises():
    """The direct regression guard for the train/serve skew."""
    contract.validate_interval("ONE_DAY", "ONE_DAY")        # matching: no raise
    contract.validate_interval("ONE_DAY", "one_day")        # case-insensitive

    try:
        contract.validate_interval("ONE_DAY", "FIVE_MINUTE", model_name="daily model")
    except contract.FeatureContractMismatch as e:
        msg = str(e)
        assert "ONE_DAY" in msg and "FIVE_MINUTE" in msg, "the error must name both intervals"
        assert "refusing" in msg.lower()
        print("ok  serving five-minute bars to a daily model raises")
        return
    raise AssertionError(
        "an interval mismatch must raise — this is the defect that made the "
        "original model produce confident nonsense"
    )


def test_only_daily_bars_are_supported():
    assert contract.SUPPORTED_INTERVALS == ("ONE_DAY",), (
        "intraday horizons were removed deliberately; supporting them again "
        "needs tick data, not a tuple entry"
    )
    print("ok  the contract supports daily bars only")


# ── Coverage reporting ─────────────────────────────────────────────────────
def test_populated_blocks_reports_an_all_null_block_as_unpopulated():
    panel = features.build_panel(_panel())
    blocks = features.populated_blocks(panel)

    assert blocks["technical"] is True
    # Nothing writes these without a point-in-time or news archive.
    for name in contract.FORWARD_ONLY_BLOCKS:
        assert blocks[name] is False, (
            f"{name} has no archive in this test, so it must report unpopulated — "
            f"a model trained without it must not appear to have used it"
        )
    print("ok  forward-only blocks report as unpopulated when their archive is empty")


def test_coverage_report_gives_a_null_rate_per_block():
    panel = features.build_panel(_panel())
    report = features.coverage_report(panel)

    assert set(report) == set(contract.BLOCKS)
    assert report["fundamental"]["null_rate"] == 1.0
    assert report["technical"]["null_rate"] < 0.5
    print("ok  coverage report gives a per-block null rate")


def test_build_panel_requires_its_input_columns():
    try:
        features.build_panel(pd.DataFrame({"symbol": ["AAA"], "date": ["2025-01-01"]}))
    except ValueError as e:
        assert "close" in str(e)
        print("ok  build_panel names the input columns it is missing")
        return
    raise AssertionError("build_panel must reject an input without OHLCV")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for fn in TESTS:
        fn()
    print(f"\n{len(TESTS)} passed")
