#!/usr/bin/env python3
"""Train price-direction models you could defend to a quant.

    python models/train.py --horizons 1day --sample     # quick check
    python models/train.py                              # full run

Replaces ``models/train_prediction.py``, which had three independent defects
that each made its reported accuracy meaningless: it trained on five-minute
bars while every serving path sent daily bars, it shuffled a time series whose
rows overlapped by 98 %, and four of its seven horizon labels were daily-bar
conventions applied to intraday data.

What is different here:
  * daily bars only, recorded in the model metadata and enforced at predict time
  * three horizons that are actually tractable, instead of seven
  * triple-barrier labels sized to clear trading cost
  * event sampling and sample-uniqueness weights
  * purged, embargoed walk-forward validation — never a shuffled split
  * isotonic calibration, so a stated 0.70 means something
  * a registry entry recording the contract, the data and the metrics

Expect the headline number to look unimpressive. An out-of-sample AUC near 0.54
on daily Indian equities is a real result; anything far above it is a leak.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

_project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_dir)

from models import labeling, registry, validation  # noqa: E402
from models.labeling import HORIZON_BARS  # noqa: E402
from services.features import (  # noqa: E402
    FEATURE_NAMES,
    FEATURE_SET_VERSION,
    build_panel,
    coverage_report,
    feature_matrix,
    populated_blocks,
)
from services.marketstore import read_range  # noqa: E402
from services.marketstore.universe import load_universe  # noqa: E402

logger = logging.getLogger("train")

INTERVAL = "ONE_DAY"
WARMUP_BARS = 200  # the sma_200 lookback; rows before this carry nulls


# ── Estimator ──────────────────────────────────────────────────────────────
def build_estimator() -> tuple[object, str]:
    """LightGBM where it loads, sklearn's histogram booster otherwise.

    LightGBM needs libomp, which the Linux Docker image bundles and a bare
    macOS install does not. Rather than fail there, fall back to sklearn's
    HistGradientBoostingClassifier — same algorithm family, no system
    dependency — and record which one was used in the registry so a model is
    never ambiguous about its own provenance.
    """
    try:
        import lightgbm as lgb

        return (
            lgb.LGBMClassifier(
                n_estimators=400, learning_rate=0.03, num_leaves=31, max_depth=6,
                min_child_samples=40, subsample=0.8, subsample_freq=1,
                colsample_bytree=0.8, reg_alpha=0.1, reg_lambda=0.5,
                random_state=42, verbosity=-1,
            ),
            "lightgbm",
        )
    except Exception as e:
        from sklearn.ensemble import HistGradientBoostingClassifier

        logger.info("lightgbm unavailable (%s); using sklearn HistGradientBoosting", e)
        return (
            HistGradientBoostingClassifier(
                max_iter=400, learning_rate=0.03, max_depth=6,
                min_samples_leaf=40, l2_regularization=0.5,
                early_stopping=False, random_state=42,
            ),
            "sklearn-hgb",
        )


def _fit(model, X, y, w):
    """Fit with sample weights, tolerating estimators that reject NaN."""
    try:
        model.fit(X, y, sample_weight=w)
    except (ValueError, TypeError):
        # LightGBM and HistGradientBoosting both handle NaN natively; this
        # guard is for any estimator substituted later that does not.
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
        model.fit(X, y, sample_weight=w)
    return model


# ── Data ───────────────────────────────────────────────────────────────────
def load_training_panel(
    *,
    symbols: list[str] | None,
    start: date | None,
    end: date | None,
    universe: pd.DataFrame,
) -> pd.DataFrame:
    raw = read_range(symbols, start, end)
    if raw.empty:
        raise SystemExit(
            "No rows in the market store. Run:\n"
            "  python -m services.marketstore.backfill --from 2020-01-01"
        )
    logger.info("loaded %d rows, %d symbols, %s -> %s",
                len(raw), raw["symbol"].nunique(),
                raw["date"].min().date(), raw["date"].max().date())

    panel = build_panel(raw, universe=universe)
    logger.info("built %d features over %d rows", len(FEATURE_NAMES), len(panel))
    return panel


def prepare_horizon(panel: pd.DataFrame, horizon: str, *, use_events: bool) -> pd.DataFrame:
    """Label, sample and weight one horizon."""
    labelled = labeling.triple_barrier_labels(panel, horizon)

    # Drop the warm-up rows: before 200 bars the long-window features are null
    # and the row teaches the model about missingness, not about markets.
    mature = labelled.groupby("symbol").cumcount() >= WARMUP_BARS
    labelled = labelled[mature & labelled[f"label_{horizon}"].notna()].copy()

    if labelled.empty:
        return labelled

    labelled["weight"] = labeling.uniqueness_weights(labelled, horizon)

    if use_events:
        events = labeling.cusum_events(labelled)
        kept = labelled[events.reindex(labelled.index, fill_value=False)]
        # Only accept event sampling if it leaves a usable dataset; on short
        # windows it can be too aggressive.
        if len(kept) >= 2000:
            logger.info("  event sampling: %d -> %d rows", len(labelled), len(kept))
            labelled = kept
        else:
            logger.info("  event sampling would leave only %d rows; keeping all", len(kept))

    return labelled.sort_values("date").reset_index(drop=True)


# ── Training ───────────────────────────────────────────────────────────────
def train_horizon(
    panel: pd.DataFrame,
    horizon: str,
    *,
    use_events: bool,
    n_splits: int,
    test_days: int,
    embargo_days: int,
) -> tuple[dict | None, dict]:
    """Walk-forward evaluate, then fit a final model on everything.

    Returns ``(bundle, diagnostics)``. The bundle is what gets served; the
    diagnostics are what you read before deciding whether to serve it.
    """
    logger.info("=== %s ===", horizon)
    data = prepare_horizon(panel, horizon, use_events=use_events)
    if len(data) < 500:
        logger.warning("  only %d labelled rows — skipping %s", len(data), horizon)
        return None, {"horizon": horizon, "skipped": "insufficient labelled rows",
                      "rows": int(len(data))}

    X = feature_matrix(data)
    y = data[f"label_{horizon}"].to_numpy(dtype="float64")
    w = data["weight"].to_numpy(dtype="float64")
    r = data[f"ret_{horizon}"].to_numpy(dtype="float64")

    label_span = max(1, HORIZON_BARS[horizon]) * 7 // 5 + 1  # trading days -> calendar
    splitter = validation.WalkForward(
        n_splits=n_splits,
        test_size_days=test_days,
        label_span_days=label_span,
        embargo_days=embargo_days,
    )

    fold_metrics: list[dict] = []
    fold_shapes: list[dict] = []

    for fold in splitter.split(data["date"]):
        model, _ = build_estimator()
        _fit(model, X[fold.train_idx], y[fold.train_idx], w[fold.train_idx])
        p = model.predict_proba(X[fold.test_idx])[:, 1]

        metrics = validation.evaluate(y[fold.test_idx], p, r[fold.test_idx])
        metrics["fold"] = fold.index
        metrics["ece"] = validation.calibration_error(metrics.get("reliability", []))
        fold_metrics.append(metrics)
        fold_shapes.append(fold.describe())

        logger.info("  fold %d: train=%d test=%d  AUC=%s  IC=%s  purged=%d",
                    fold.index, len(fold.train_idx), len(fold.test_idx),
                    metrics.get("auc"), metrics.get("ic"), fold.purged)

    if not fold_metrics:
        logger.warning("  no usable folds for %s", horizon)
        return None, {"horizon": horizon, "skipped": "no usable folds"}

    agg = validation.aggregate(fold_metrics)
    logger.info("  walk-forward: AUC=%.4f (sd %.4f)  IC=%s  over %d folds",
                agg.get("auc", float("nan")), agg.get("auc_std", 0.0),
                agg.get("ic"), agg["folds"])

    # Final fit on everything except a calibration tail, then calibrate on that
    # tail. Calibrating on data the model trained on would make the reliability
    # curve look perfect and mean nothing.
    cut = int(len(data) * 0.85)
    final, estimator_name = build_estimator()
    _fit(final, X[:cut], y[:cut], w[:cut])

    calibrator = None
    if cut < len(data) - 50:
        from sklearn.isotonic import IsotonicRegression

        p_cal = final.predict_proba(X[cut:])[:, 1]
        calibrator = IsotonicRegression(out_of_bounds="clip").fit(p_cal, y[cut:])
        before = validation.calibration_error(
            validation.evaluate(y[cut:], p_cal, r[cut:]).get("reliability", []))
        after = validation.calibration_error(
            validation.evaluate(y[cut:], calibrator.predict(p_cal), r[cut:]).get("reliability", []))
        logger.info("  calibration error on hold-out: %s -> %s", before, after)

    bundle = {
        "model": final,
        "calibrator": calibrator,
        "horizon": horizon,
        "estimator": estimator_name,
        "feature_names": list(FEATURE_NAMES),
        "feature_set_version": FEATURE_SET_VERSION,
        "interval": INTERVAL,
    }
    diagnostics = {
        "horizon": horizon,
        "rows": int(len(data)),
        "label_summary": labeling.label_summary(data, horizon),
        "walk_forward": agg,
        "folds": fold_shapes,
        "fold_metrics": [
            {k: v for k, v in m.items() if k != "reliability"} for m in fold_metrics
        ],
        "reliability_last_fold": fold_metrics[-1].get("reliability", []),
        "calibrated": calibrator is not None,
    }
    return bundle, diagnostics


# ── Reporting ──────────────────────────────────────────────────────────────
def build_report(meta_dict: dict, diagnostics: list[dict]) -> str:
    lines = [
        "# Training report",
        "",
        f"- **Version**: `{meta_dict['version']}`",
        f"- **Trained at**: {meta_dict['trained_at']}",
        f"- **Interval**: {meta_dict['interval']}  (enforced at predict time)",
        f"- **Feature set**: v{meta_dict['feature_set_version']}, "
        f"{len(meta_dict['feature_names'])} features",
        f"- **Estimator**: {meta_dict['estimator']}",
        f"- **Universe**: {meta_dict['universe_size']} symbols",
        f"- **Date range**: {meta_dict['date_range'][0]} to {meta_dict['date_range'][1]}",
        f"- **Git**: `{meta_dict.get('git_sha')}`",
        "",
        "## Feature block coverage",
        "",
        "| Block | Populated | Null rate |",
        "|---|---|---|",
    ]
    cov = meta_dict.get("coverage", {})
    for block, populated in (meta_dict.get("populated_blocks") or {}).items():
        null_rate = cov.get(block, {}).get("null_rate")
        lines.append(f"| {block} | {'yes' if populated else 'NO'} | "
                     f"{null_rate if null_rate is not None else '-'} |")

    unpopulated = [k for k, v in (meta_dict.get("populated_blocks") or {}).items() if not v]
    if unpopulated:
        lines += [
            "",
            f"> **{', '.join(unpopulated)}** had no values in this training window. "
            "These blocks are forward-only: point-in-time fundamentals and scored news "
            "accumulate from the day capture began, and cannot be reconstructed for "
            "historical dates without look-ahead bias. Retrain once the archive is deep "
            "enough. This model does not use them.",
        ]

    lines += ["", "## Walk-forward results", ""]
    for d in diagnostics:
        if d.get("skipped"):
            lines.append(f"### {d['horizon']} — skipped ({d['skipped']})")
            continue
        wf = d["walk_forward"]
        ls = d.get("label_summary", {})
        lines += [
            f"### {d['horizon']}",
            "",
            f"- rows: {d['rows']}, folds: {wf.get('folds')}, "
            f"test rows: {wf.get('total_test_rows')}",
            f"- positive rate: {ls.get('positive_rate')}, "
            f"barrier hit rate: {ls.get('barrier_hit_rate')}",
            f"- **AUC {wf.get('auc')}** (sd {wf.get('auc_std')})",
            f"- IC {wf.get('ic')}, precision@20% {wf.get('precision_at_k')}",
            f"- Brier {wf.get('brier')}, net mean return "
            f"{wf.get('net_mean_return')}, net hit rate {wf.get('net_hit_rate')}",
            f"- calibrated: {d.get('calibrated')}",
            "",
        ]

    lines += [
        "## How to read this",
        "",
        "AUC is measured with purged, embargoed walk-forward folds — training rows whose",
        "label window reaches into a test block are removed, and a gap is left after it.",
        "An AUC near 0.50 means no signal was found in this window; 0.53-0.58 is a real,",
        "usable result for daily Indian equities. A number far above that is almost always",
        "a leak rather than an edge, and should be investigated as a bug.",
        "",
        "The fold-level spread matters as much as the mean: a mean of 0.55 made of 0.49",
        "and 0.61 is a much weaker claim than one made of 0.54 and 0.56.",
    ]
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--horizons", nargs="+", default=list(HORIZON_BARS),
                        choices=list(HORIZON_BARS))
    parser.add_argument("--symbols", nargs="+", help="override the universe")
    parser.add_argument("--sample", action="store_true",
                        help="60 symbols, 3 folds — a fast smoke test, not a real model")
    parser.add_argument("--from", dest="start", help="start date YYYY-MM-DD")
    parser.add_argument("--to", dest="end", help="end date YYYY-MM-DD")
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--embargo-days", type=int, default=5)
    parser.add_argument("--no-events", action="store_true",
                        help="disable CUSUM event sampling")
    parser.add_argument("--no-register", action="store_true",
                        help="evaluate and report, but do not save the model")
    parser.add_argument("--notes", default="")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    universe = load_universe()
    symbols = args.symbols or universe["symbol"].tolist()
    if args.sample:
        symbols = symbols[:60]
        args.splits = min(args.splits, 3)
        logger.info("sample mode: %d symbols, %d folds", len(symbols), args.splits)

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None

    panel = load_training_panel(symbols=symbols, start=start, end=end, universe=universe)

    blocks = populated_blocks(panel)
    coverage = coverage_report(panel)
    for name, ok in blocks.items():
        if not ok:
            logger.warning("feature block '%s' is entirely null in this window", name)

    bundles: dict[str, dict] = {}
    diagnostics: list[dict] = []
    estimator_name = "unknown"

    for horizon in args.horizons:
        bundle, diag = train_horizon(
            panel, horizon,
            use_events=not args.no_events,
            n_splits=args.splits,
            test_days=args.test_days,
            embargo_days=args.embargo_days,
        )
        diagnostics.append(diag)
        if bundle:
            bundles[horizon] = bundle
            estimator_name = bundle["estimator"]

    if not bundles:
        logger.error("no horizon produced a model — nothing registered")
        print(json.dumps(diagnostics, indent=2, default=str))
        return 1

    version = registry.new_version()
    meta = registry.ModelMeta(
        version=version,
        trained_at=datetime.now(timezone.utc).isoformat(),
        interval=INTERVAL,
        feature_set_version=FEATURE_SET_VERSION,
        feature_names=list(FEATURE_NAMES),
        horizons=sorted(bundles),
        estimator=estimator_name,
        universe_size=int(panel["symbol"].nunique()),
        date_range=[str(panel["date"].min().date()), str(panel["date"].max().date())],
        rows_trained=int(len(panel)),
        populated_blocks=blocks,
        coverage=coverage,
        cv_metrics={d["horizon"]: d.get("walk_forward", {}) for d in diagnostics
                    if not d.get("skipped")},
        label_summary={d["horizon"]: d.get("label_summary", {}) for d in diagnostics
                       if not d.get("skipped")},
        calibrated=any(b.get("calibrator") is not None for b in bundles.values()),
        git_sha=registry.git_sha(),
        notes=args.notes or ("sample run — not a production model" if args.sample else ""),
    )

    report_md = build_report(meta.to_dict(), diagnostics)

    if args.no_register:
        print(report_md)
        logger.info("--no-register: nothing saved")
        return 0

    vdir = registry.save(version, meta, bundles, report_md=report_md,
                         make_current=not args.sample)
    with open(os.path.join(vdir, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta.to_dict(), "diagnostics": diagnostics},
                  f, indent=2, default=str)

    print("\n" + report_md)
    print(f"\nartifacts: {vdir}")
    if args.sample:
        print("sample run — CURRENT not updated; serve with an explicit version")
    return 0


if __name__ == "__main__":
    sys.exit(main())
