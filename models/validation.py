"""Purged, embargoed walk-forward cross-validation, and the metrics worth reporting.

The defect this replaces: ``train_test_split(X, y, shuffle=True)`` on rows
built by sliding a window forward one bar at a time. Adjacent rows shared 49 of
their 50 bars of history and their forward label windows overlapped almost
entirely, so shuffling scattered near-duplicates across both sides of the
split. The reported accuracy measured how well the model recognised rows it had
effectively already seen.

Three mechanisms make a split honest here:

  * **time-ordered folds** — the test block always lies in the future of its
    training block, which is the only arrangement that matches how the model
    will be used
  * **purging** — a training row whose label window reaches into the test
    period has seen the test period's outcome, so it is removed
  * **embargo** — a gap after the test block before training resumes, because
    serial correlation leaks across the boundary even without direct overlap

Expect measured performance to fall when switching to this. That drop is the
measurement becoming real, not a regression.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class Fold:
    """One walk-forward fold, as positional indices into the sorted panel."""

    index: int
    train_idx: np.ndarray
    test_idx: np.ndarray
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    purged: int = 0
    embargoed: int = 0

    def describe(self) -> dict:
        return {
            "fold": self.index,
            "train_rows": int(len(self.train_idx)),
            "test_rows": int(len(self.test_idx)),
            "train_end": self.train_end.date().isoformat(),
            "test_start": self.test_start.date().isoformat(),
            "test_end": self.test_end.date().isoformat(),
            "purged_rows": int(self.purged),
            "embargoed_rows": int(self.embargoed),
        }


@dataclass
class WalkForward:
    """Expanding-window walk-forward splitter with purging and embargo.

    ``label_span_days`` is how far a label reaches forward in calendar days;
    training rows whose span crosses into the test window are purged.
    """

    n_splits: int = 5
    test_size_days: int = 60
    label_span_days: int = 5
    embargo_days: int = 5
    min_train_days: int = 250
    folds_: list[Fold] = field(default_factory=list)

    def split(self, dates: pd.Series) -> Iterator[Fold]:
        """Yield folds for a panel sorted by date.

        ``dates`` is the per-row date column, positionally aligned with the
        feature matrix.
        """
        d = pd.to_datetime(pd.Series(dates).reset_index(drop=True))
        unique_days = np.array(sorted(d.unique()))
        if len(unique_days) == 0:
            return

        first_day = pd.Timestamp(unique_days[0])
        last_day = pd.Timestamp(unique_days[-1])

        earliest_test_start = first_day + pd.Timedelta(days=self.min_train_days)
        if earliest_test_start >= last_day:
            # Not enough history for the requested warm-up. Fall back to using
            # the first 60 % as training rather than silently yielding nothing.
            earliest_test_start = first_day + (last_day - first_day) * 0.6
            logger.warning(
                "walk-forward: only %d days of data; reducing the minimum "
                "training window so folds can be formed at all",
                (last_day - first_day).days,
            )

        span = (last_day - earliest_test_start).days
        if span <= 0:
            return

        step = max(1, span // max(1, self.n_splits))
        test_size = pd.Timedelta(days=self.test_size_days)
        label_span = pd.Timedelta(days=self.label_span_days)
        embargo = pd.Timedelta(days=self.embargo_days)

        self.folds_ = []
        for k in range(self.n_splits):
            test_start = earliest_test_start + pd.Timedelta(days=step * k)
            test_end = test_start + test_size
            if test_start >= last_day:
                break
            test_end = min(test_end, last_day)

            # Purge: a training row dated within label_span of test_start has a
            # label window that reaches into the test block.
            train_cutoff = test_start - label_span

            train_mask = d < train_cutoff
            test_mask = (d >= test_start) & (d <= test_end)

            # Embargo trims the tail of training that sits closest to the test
            # block, on top of the purge.
            embargo_mask = (d >= train_cutoff - embargo) & (d < train_cutoff)

            purged = int(((d >= train_cutoff) & (d < test_start)).sum())
            embargoed = int(embargo_mask.sum())
            train_mask = train_mask & ~embargo_mask

            train_idx = np.flatnonzero(train_mask.to_numpy())
            test_idx = np.flatnonzero(test_mask.to_numpy())

            if len(train_idx) == 0 or len(test_idx) == 0:
                continue

            fold = Fold(
                index=k,
                train_idx=train_idx,
                test_idx=test_idx,
                train_end=pd.Timestamp(d.iloc[train_idx[-1]]),
                test_start=pd.Timestamp(d.iloc[test_idx[0]]),
                test_end=pd.Timestamp(d.iloc[test_idx[-1]]),
                purged=purged,
                embargoed=embargoed,
            )
            self.folds_.append(fold)
            yield fold


# ── Metrics ────────────────────────────────────────────────────────────────
def evaluate(
    y_true: np.ndarray,
    p_pred: np.ndarray,
    returns: Optional[np.ndarray] = None,
    *,
    cost: float = 0.0035,
    top_k_frac: float = 0.2,
) -> dict:
    """The metric stack that replaces a single accuracy number.

    Accuracy is reported last and only for continuity with the old output; it
    is the least informative number here.
    """
    y = np.asarray(y_true, dtype="float64")
    p = np.asarray(p_pred, dtype="float64")
    mask = np.isfinite(y) & np.isfinite(p)
    y, p = y[mask], p[mask]

    if len(y) == 0 or len(np.unique(y)) < 2:
        return {"n": int(len(y)), "auc": None, "note": "single-class or empty test block"}

    out: dict = {
        "n": int(len(y)),
        "base_rate": round(float(y.mean()), 4),
        "auc": round(_auc(y, p), 4),
        "brier": round(float(np.mean((p - y) ** 2)), 5),
        "accuracy": round(float(((p > 0.5).astype(float) == y).mean()), 4),
    }

    # Information coefficient: rank correlation between the prediction and the
    # realised forward return. The standard buy-side measure of signal.
    if returns is not None:
        r = np.asarray(returns, dtype="float64")[mask]
        ok = np.isfinite(r)
        if ok.sum() > 10:
            out["ic"] = round(_spearman(p[ok], r[ok]), 4)
            out.update(_net_performance(p[ok], r[ok], cost=cost, top_k_frac=top_k_frac))

    out["precision_at_k"] = round(_precision_at_k(y, p, top_k_frac), 4)
    out["reliability"] = _reliability(y, p)
    return out


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    """Rank-based AUC; equal to the Mann-Whitney U statistic, ties averaged."""
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p), dtype="float64")
    ranks[order] = np.arange(1, len(p) + 1, dtype="float64")

    # Average ranks within ties so a constant prediction scores exactly 0.5.
    sorted_p = p[order]
    i = 0
    while i < len(sorted_p):
        j = i
        while j + 1 < len(sorted_p) and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = ranks[order[i:j + 1]].mean()
        i = j + 1

    n_pos = float(y.sum())
    n_neg = float(len(y) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return 0.5
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def _precision_at_k(y: np.ndarray, p: np.ndarray, frac: float) -> float:
    """Hit rate among the highest-scoring fraction — what you get if you act on top-k."""
    k = max(1, int(len(p) * frac))
    top = np.argsort(p)[::-1][:k]
    return float(y[top].mean())


def _net_performance(p: np.ndarray, r: np.ndarray, *, cost: float, top_k_frac: float) -> dict:
    """Return of a long-only top-k rule, after a round-trip cost.

    Crude by design: one cost per position, no sizing, no capacity. It exists
    to answer "does the edge survive a realistic fee", not to be a backtest.
    """
    k = max(1, int(len(p) * top_k_frac))
    top = np.argsort(p)[::-1][:k]
    gross = r[top]
    net = gross - cost

    out = {
        "net_mean_return": round(float(net.mean()), 5),
        "net_hit_rate": round(float((net > 0).mean()), 4),
    }
    if net.std() > 0:
        # Per-trade Sharpe; not annualised, because the horizon varies.
        out["net_sharpe_per_trade"] = round(float(net.mean() / net.std()), 4)
    return out


def _reliability(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    """Calibration curve: predicted probability vs realised frequency per bin."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    out = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        sel = (p >= lo) & (p < hi if i < bins - 1 else p <= hi)
        if sel.sum() == 0:
            continue
        out.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "n": int(sel.sum()),
            "predicted": round(float(p[sel].mean()), 4),
            "observed": round(float(y[sel].mean()), 4),
        })
    return out


def calibration_error(reliability: list[dict]) -> Optional[float]:
    """Expected calibration error: mean |predicted - observed|, weighted by bin size."""
    if not reliability:
        return None
    total = sum(b["n"] for b in reliability)
    if total == 0:
        return None
    return round(
        sum(b["n"] * abs(b["predicted"] - b["observed"]) for b in reliability) / total, 4
    )


def aggregate(fold_metrics: list[dict]) -> dict:
    """Mean of each numeric metric across folds, plus its spread.

    The spread matters: a mean AUC of 0.55 made of 0.49 and 0.61 is a different
    claim from one made of 0.54 and 0.56.
    """
    numeric_keys = [
        "auc", "brier", "accuracy", "ic", "precision_at_k",
        "net_mean_return", "net_hit_rate", "net_sharpe_per_trade", "base_rate",
    ]
    out: dict = {"folds": len(fold_metrics)}
    for key in numeric_keys:
        vals = [m[key] for m in fold_metrics
                if isinstance(m.get(key), (int, float))]
        if vals:
            out[key] = round(float(np.mean(vals)), 4)
            out[f"{key}_std"] = round(float(np.std(vals)), 4)
    out["total_test_rows"] = int(sum(m.get("n", 0) for m in fold_metrics))
    return out
