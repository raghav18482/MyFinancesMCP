"""Feature assembly: six blocks, one versioned contract, one entry point.

    from services.features import build_panel, FEATURE_NAMES

    panel = build_panel(read_range(symbols, start, end))

Block order matters — cross-sectional ranks need the technical block, market
context needs returns, so ``build_panel`` runs them in dependency order.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from services.features import (
    cross_sectional,
    flow,
    fundamental,
    market_context,
    sentiment,
    technical,
)
from services.features.contract import (
    BLOCKS,
    FEATURE_NAMES,
    FEATURE_SET_VERSION,
    FORWARD_ONLY_BLOCKS,
    SUPPORTED_INTERVALS,
    FeatureContractMismatch,
    validate_frame_columns,
    validate_interval,
    validate_version,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BLOCKS",
    "FEATURE_NAMES",
    "FEATURE_SET_VERSION",
    "FORWARD_ONLY_BLOCKS",
    "SUPPORTED_INTERVALS",
    "FeatureContractMismatch",
    "build_panel",
    "feature_matrix",
    "populated_blocks",
    "validate_interval",
    "validate_version",
]

REQUIRED_INPUT_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume"]


def build_panel(
    panel: pd.DataFrame,
    *,
    universe: pd.DataFrame | None = None,
    pit_root: str | None = None,
    news_root: str | None = None,
    include: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Build every contract feature onto a tidy symbol x date frame.

    ``include`` restricts which blocks run; omitted blocks still get their
    columns, as nulls, so the frame always matches the contract.
    """
    missing = [c for c in REQUIRED_INPUT_COLUMNS if c not in panel.columns]
    if missing:
        raise ValueError(f"build_panel: input is missing {missing}")

    if panel.empty:
        return panel.assign(**{f: np.nan for f in FEATURE_NAMES})

    wanted = set(include) if include is not None else set(BLOCKS)
    out = panel.sort_values(["symbol", "date"]).reset_index(drop=True).copy()
    out["date"] = pd.to_datetime(out["date"])

    # Order is a dependency order, not a preference.
    if "technical" in wanted:
        out = technical.build(out)
    if "cross_sectional" in wanted:
        out = cross_sectional.build(out, universe)
    elif "industry" not in out.columns:
        # Market context needs the industry column even when ranks are skipped.
        out = cross_sectional.attach_industry(out, universe)
    if "market_context" in wanted:
        out = market_context.build(out)
    if "flow" in wanted:
        out = flow.build(out)
    if "fundamental" in wanted:
        out = fundamental.build(out, pit_root)
    if "sentiment" in wanted:
        out = sentiment.build(out, news_root)

    for name in validate_frame_columns(out.columns):
        out[name] = np.nan

    return out.sort_values(["symbol", "date"]).reset_index(drop=True)


def feature_matrix(panel: pd.DataFrame) -> np.ndarray:
    """Contract features as a float array, in contract order.

    Infinities become NaN rather than surviving into the model: a divide-by-zero
    in a ratio feature should be absent, not enormous.
    """
    missing = validate_frame_columns(panel.columns)
    if missing:
        raise FeatureContractMismatch(
            f"panel is missing {len(missing)} contract features: {missing[:8]}"
        )
    arr = panel[FEATURE_NAMES].to_numpy(dtype="float64", copy=True)
    arr[~np.isfinite(arr)] = np.nan
    return arr


def populated_blocks(panel: pd.DataFrame) -> dict[str, bool]:
    """Which blocks carry real values, for the model registry.

    The forward-only blocks are null for historical rows by design, and a model
    trained without them must say so rather than implying full coverage.
    """
    out: dict[str, bool] = {}
    for name, feats in BLOCKS.items():
        cols = [c for c in feats if c in panel.columns]
        out[name] = bool(panel[cols].notna().any().any()) if cols else False
    return out


def coverage_report(panel: pd.DataFrame) -> dict:
    """Null rate per block, for the training report."""
    report = {}
    for name, feats in BLOCKS.items():
        cols = [c for c in feats if c in panel.columns]
        if not cols:
            report[name] = {"features": 0, "null_rate": 1.0}
            continue
        null_rate = float(panel[cols].isna().to_numpy().mean())
        report[name] = {"features": len(cols), "null_rate": round(null_rate, 4)}
    return report
