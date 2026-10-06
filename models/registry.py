"""Model registry: a trained model is never saved without its contract.

The old pipeline did ``joblib.dump(model, path)`` — a bare estimator with no
record of what it was trained on. Nothing then prevented serving it daily bars
when it had been trained on five-minute bars, because nothing knew. The fix is
structural: a model is saved as a bundle carrying everything needed to decide
whether it may be used, and the loader checks before handing it over.

Layout::

    models/artifacts/
      <version>/
        meta.json          the contract and the metrics
        model_1day.joblib  estimator + calibrator per horizon
        report.md          human-readable summary
      CURRENT              a file naming the active version
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACT_ROOT = os.path.join(_repo_root, "models", "artifacts")
CURRENT_FILE = os.path.join(ARTIFACT_ROOT, "CURRENT")
META_NAME = "meta.json"


@dataclass
class ModelMeta:
    """Everything needed to decide whether a model may be used on some input."""

    version: str
    trained_at: str
    interval: str                      # the candle interval it was trained on
    feature_set_version: int
    feature_names: list[str]
    horizons: list[str]
    estimator: str                     # lightgbm | sklearn-hgb
    universe_size: int
    date_range: list[str]              # [first, last]
    rows_trained: int
    populated_blocks: dict[str, bool]  # which feature blocks had real values
    coverage: dict[str, Any] = field(default_factory=dict)
    cv_metrics: dict[str, Any] = field(default_factory=dict)
    label_summary: dict[str, Any] = field(default_factory=dict)
    calibrated: bool = False
    git_sha: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def new_version() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def git_sha() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_repo_root, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def version_dir(version: str, root: str = ARTIFACT_ROOT) -> str:
    return os.path.join(root, version)


def save(
    version: str,
    meta: ModelMeta,
    bundles: dict[str, dict],
    *,
    root: str = ARTIFACT_ROOT,
    make_current: bool = True,
    report_md: str = "",
) -> str:
    """Persist one training run. ``bundles`` maps horizon -> {model, calibrator}."""
    import joblib

    vdir = version_dir(version, root)
    os.makedirs(vdir, exist_ok=True)

    for horizon, bundle in bundles.items():
        joblib.dump(bundle, os.path.join(vdir, f"model_{horizon}.joblib"))

    with open(os.path.join(vdir, META_NAME), "w", encoding="utf-8") as f:
        json.dump(meta.to_dict(), f, indent=2, default=str)

    if report_md:
        with open(os.path.join(vdir, "report.md"), "w", encoding="utf-8") as f:
            f.write(report_md)

    if make_current:
        set_current(version, root)

    logger.info("registry: saved %s (%d horizons) to %s", version, len(bundles), vdir)
    return vdir


def set_current(version: str, root: str = ARTIFACT_ROOT) -> None:
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "CURRENT"), "w", encoding="utf-8") as f:
        f.write(version.strip() + "\n")


def current_version(root: str = ARTIFACT_ROOT) -> Optional[str]:
    path = os.path.join(root, "CURRENT")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            v = f.read().strip()
        return v or None
    except Exception:
        return None


def list_versions(root: str = ARTIFACT_ROOT) -> list[str]:
    if not os.path.isdir(root):
        return []
    return sorted(
        name for name in os.listdir(root)
        if os.path.exists(os.path.join(root, name, META_NAME))
    )


def load_meta(version: Optional[str] = None, root: str = ARTIFACT_ROOT) -> Optional[ModelMeta]:
    version = version or current_version(root)
    if not version:
        return None
    path = os.path.join(version_dir(version, root), META_NAME)
    if not os.path.exists(path):
        logger.warning("registry: no metadata for version %s", version)
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        known = {k: v for k, v in raw.items() if k in ModelMeta.__dataclass_fields__}
        return ModelMeta(**known)
    except Exception as e:
        logger.warning("registry: metadata for %s unreadable (%s)", version, e)
        return None


def load_bundles(
    version: Optional[str] = None,
    root: str = ARTIFACT_ROOT,
) -> tuple[Optional[ModelMeta], dict[str, dict]]:
    """Load the metadata and every horizon bundle for a version.

    Returns ``(None, {})`` when nothing is registered — the caller then falls
    back to the heuristic and must say so.
    """
    meta = load_meta(version, root)
    if meta is None:
        return None, {}

    try:
        import joblib
    except ImportError:
        logger.warning("registry: joblib unavailable; cannot load models")
        return meta, {}

    vdir = version_dir(meta.version, root)
    bundles: dict[str, dict] = {}
    for horizon in meta.horizons:
        path = os.path.join(vdir, f"model_{horizon}.joblib")
        if not os.path.exists(path):
            logger.warning("registry: %s missing model for %s", meta.version, horizon)
            continue
        try:
            bundles[horizon] = joblib.load(path)
        except Exception as e:
            logger.warning("registry: failed to load %s/%s (%s)", meta.version, horizon, e)

    return meta, bundles


def describe(version: Optional[str] = None, root: str = ARTIFACT_ROOT) -> dict:
    """A compact summary for the API and the CLI."""
    meta = load_meta(version, root)
    if meta is None:
        return {"registered": False, "reason": "no model registered"}

    unpopulated = [k for k, v in (meta.populated_blocks or {}).items() if not v]
    return {
        "registered": True,
        "version": meta.version,
        "trained_at": meta.trained_at,
        "interval": meta.interval,
        "feature_set_version": meta.feature_set_version,
        "horizons": meta.horizons,
        "estimator": meta.estimator,
        "calibrated": meta.calibrated,
        "rows_trained": meta.rows_trained,
        "date_range": meta.date_range,
        "unpopulated_blocks": unpopulated,
        "cv_metrics": meta.cv_metrics,
        "git_sha": meta.git_sha,
    }
