"""Fundamental features, read strictly through the point-in-time store.

``services/fundamental_scoring.py`` produces a careful 0-100 score with four
pillar sub-scores, sector overrides for lenders, and a coverage-based
confidence — and the model has never seen any of it. This block wires it in.

The one rule that matters: values come from ``marketstore.pit``, which returns
only snapshots observed on or before the row's date. There is deliberately no
fallback to a live fetch. A live fetch would supply today's restated figures
for a 2024 row, which is exactly the look-ahead bias that makes a backtest look
wonderful and a live model fail.

Consequence, stated plainly: for dates before point-in-time capture began, this
block is entirely null. That is the correct answer, and the training run
records which blocks were populated rather than hiding it.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from services.features.contract import FUNDAMENTAL

logger = logging.getLogger(__name__)

# Snapshot column -> feature name.
_FIELD_MAP = {
    "score_total": "fund_score_total",
    "score_confidence": "fund_coverage",
    "pillar_valuation": "fund_pillar_valuation",
    "pillar_profitability": "fund_pillar_profitability",
    "pillar_balance_sheet": "fund_pillar_balance_sheet",
    "pillar_growth": "fund_pillar_growth",
    "pe_ratio": "fund_pe_ratio",
    "pb_ratio": "fund_pb_ratio",
    "roe": "fund_roe",
    "debt_to_equity": "fund_debt_to_equity",
    "revenue_growth": "fund_revenue_growth",
    "earnings_growth": "fund_earnings_growth",
}


def build(panel: pd.DataFrame, pit_root: str | None = None) -> pd.DataFrame:
    """Join point-in-time fundamentals onto the panel."""
    if panel.empty:
        return panel.assign(**{f: np.nan for f in FUNDAMENTAL})

    out = panel.copy()
    snaps = _load_snapshots(pit_root)

    if snaps.empty:
        logger.info(
            "fundamental: no point-in-time snapshots yet — block will be null. "
            "Snapshots accumulate as the app fetches fundamentals."
        )
        for f in FUNDAMENTAL:
            out[f] = np.nan
        return out

    out = out.sort_values(["date", "symbol"])
    snaps = snaps.sort_values("observed_at")

    # merge_asof with direction="backward" is the point-in-time join: for each
    # row take the newest snapshot at or before its date, and nothing after.
    merged = pd.merge_asof(
        out,
        snaps[["symbol", "observed_at"] + list(_FIELD_MAP)],
        left_on="date",
        right_on="observed_at",
        by="symbol",
        direction="backward",
    )

    merged = merged.rename(columns=_FIELD_MAP)

    # How old the figures are. A model can learn to discount a stale snapshot
    # instead of treating a two-year-old P/E as current.
    age = (pd.to_datetime(merged["date"]) - pd.to_datetime(merged["observed_at"])).dt.days
    merged["fund_is_stale_days"] = age

    merged = merged.drop(columns=["observed_at"], errors="ignore")

    for f in FUNDAMENTAL:
        if f not in merged.columns:
            merged[f] = np.nan

    return merged.sort_values(["symbol", "date"]).reset_index(drop=True)


def _load_snapshots(pit_root: str | None) -> pd.DataFrame:
    try:
        from services.marketstore import pit

        return pit.read_all(pit_root) if pit_root else pit.read_all()
    except Exception as e:
        logger.warning("fundamental: point-in-time store unavailable (%s)", e)
        return pd.DataFrame()


def block_is_populated(panel: pd.DataFrame) -> bool:
    """Whether any fundamental feature carries real values in this panel.

    The training run calls this and records the answer in the model registry,
    so a model trained before point-in-time history existed cannot later be
    mistaken for one that used fundamentals.
    """
    cols = [c for c in FUNDAMENTAL if c in panel.columns]
    if not cols:
        return False
    return bool(panel[cols].notna().any().any())
