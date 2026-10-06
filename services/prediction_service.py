"""Price-direction prediction: registry-backed, interval-strict, calibrated.

Three defects in the previous version are fixed structurally rather than by
being careful:

  * **Train/serve skew.** Models were trained on five-minute bars and served
    daily bars under identical feature names, so nothing ever errored. A model
    now carries the interval it was trained on and ``predict_direction``
    refuses to run on anything else.
  * **A silent heuristic.** No trained artefact existed, so a hand-weighted
    rule score served every request while the UI called it LightGBM. The
    response now always states which engine produced it, and the fallback is
    labelled rather than disguised.
  * **Uncalibrated confidence.** ``max(p, 1-p)`` was presented as a
    probability. Predictions now pass through a calibrator fitted on held-out
    data, and the response says whether that happened.

Feature context matters at serve time: cross-sectional ranks and market context
need the whole universe on the same date, not one symbol's candles. Where the
local market store can supply that, it is used; where it cannot, those features
are null and the response says so instead of pretending otherwise.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

from models.labeling import HORIZONS
from services.features import (
    FEATURE_NAMES,
    FEATURE_SET_VERSION,
    FeatureContractMismatch,
    build_panel,
    feature_matrix,
    validate_interval,
    validate_version,
)

logger = logging.getLogger(__name__)

# Kept as module constants for callers and tests. The seven-horizon set is
# gone: four of its entries were daily-bar conventions applied to intraday
# data, and the three intraday horizons are not tractable from candles at all.
TIMEFRAMES = list(HORIZONS)

DEFAULT_INTERVAL = "ONE_DAY"

# How many trailing sessions of universe context to load when building
# cross-sectional features. 260 covers the 200-bar warm-up with room to spare.
CONTEXT_SESSIONS = 260

_CACHE_TTL = 120
_cache: dict[str, dict] = {}

_registry_cache: dict[str, Any] = {"loaded": False, "meta": None, "bundles": {}}


# ── Cache ──────────────────────────────────────────────────────────────────
def _cache_key(symbol: str, candles: list, interval: str) -> str:
    """Key on the actual window, not its length.

    The previous key hashed ``len(candles)``, so two different windows of equal
    length collided. The 120-second TTL limited the damage but the key was
    simply wrong.
    """
    last = candles[-1][0] if candles else ""
    first = candles[0][0] if candles else ""
    raw = f"{symbol}|{interval}|{first}|{last}|{len(candles)}"
    return "pred:" + hashlib.md5(raw.encode()).hexdigest()


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: str, data) -> None:
    _cache[key] = {"data": data, "ts": time.time()}


# ── Model loading ──────────────────────────────────────────────────────────
def _load_registry(force: bool = False) -> tuple[Optional[Any], dict]:
    """Lazily load the registered model bundles. Empty dict means heuristic."""
    if _registry_cache["loaded"] and not force:
        return _registry_cache["meta"], _registry_cache["bundles"]

    try:
        from models import registry

        meta, bundles = registry.load_bundles()
    except Exception as e:
        logger.warning("prediction: registry unavailable (%s)", e)
        meta, bundles = None, {}

    if meta is None:
        logger.info("prediction: no registered model — serving the rule-based "
                    "heuristic, which the response labels as such")
    else:
        try:
            validate_version(meta.feature_set_version, model_name=f"model {meta.version}")
        except FeatureContractMismatch as e:
            logger.error("prediction: %s — refusing to serve it", e)
            meta, bundles = None, {}

    _registry_cache.update({"loaded": True, "meta": meta, "bundles": bundles})
    return meta, bundles


def reload_models() -> dict:
    """Drop the cached bundles and re-read the registry. For tests and admin."""
    _registry_cache.update({"loaded": False, "meta": None, "bundles": {}})
    _cache.clear()
    meta, bundles = _load_registry(force=True)
    return {"registered": meta is not None,
            "version": getattr(meta, "version", None),
            "horizons": sorted(bundles)}


def model_status() -> dict:
    """What is actually serving right now. Surfaced by the API and the UI."""
    meta, bundles = _load_registry()
    if meta is None:
        return {
            "model_type": "heuristic",
            "registered": False,
            "detail": "No trained model is registered. Predictions come from a "
                      "rule-based technical score, not from machine learning.",
        }
    return {
        "model_type": meta.estimator,
        "registered": True,
        "version": meta.version,
        "interval": meta.interval,
        "horizons": sorted(bundles),
        "calibrated": meta.calibrated,
        "trained_at": meta.trained_at,
        "unpopulated_blocks": [k for k, v in (meta.populated_blocks or {}).items() if not v],
    }


# ── Feature building at serve time ─────────────────────────────────────────
def _panel_from_candles(candles: list, symbol: str) -> pd.DataFrame:
    """Angel-style ``[ts, o, h, l, c, v]`` rows into the store's panel shape."""
    df = pd.DataFrame(candles, columns=["date", "open", "high", "low", "close", "volume"])
    df["symbol"] = _norm(symbol)
    df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_localize(None)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)


def _build_features(candles: list, symbol: str) -> tuple[Optional[pd.Series], str, dict]:
    """Build one row of contract features for ``symbol``.

    Returns ``(row, context, info)`` where context is:
      ``universe``       full cross-sectional and market context available
      ``single_symbol``  only per-symbol features; the rest are null
    """
    sym = _norm(symbol)
    own = _panel_from_candles(candles, sym)
    if own.empty:
        return None, "none", {"reason": "no usable candles"}

    as_of = own["date"].max()

    # Preferred path: pull the universe around this date from the local store so
    # cross-sectional ranks and market context are real rather than null.
    try:
        from services.marketstore import read_range
        from services.marketstore.universe import load_universe

        universe = load_universe()
        start = (as_of - timedelta(days=int(CONTEXT_SESSIONS * 1.6))).date()
        context_panel = read_range(universe["symbol"].tolist(), start, as_of.date())

        if not context_panel.empty and sym in set(context_panel["symbol"]):
            panel = build_panel(context_panel, universe=universe)
            rows = panel[panel["symbol"] == sym]
            if not rows.empty:
                return rows.iloc[-1], "universe", {
                    "as_of": str(pd.Timestamp(rows["date"].iloc[-1]).date()),
                    "context_symbols": int(context_panel["symbol"].nunique()),
                }
    except Exception as e:
        logger.debug("prediction: universe context unavailable (%s)", e)

    # Fallback: this symbol alone. Cross-sectional and market-context features
    # cannot be computed from one series, so they stay null — which the model
    # handles natively and the response reports.
    if len(own) < 60:
        return None, "none", {"reason": f"only {len(own)} candles; need at least 60"}

    panel = build_panel(own, include=("technical", "flow"))
    return panel.iloc[-1], "single_symbol", {
        "as_of": str(pd.Timestamp(panel["date"].iloc[-1]).date()),
        "note": "cross-sectional and market-context features unavailable for a "
                "single symbol; they were passed to the model as missing",
    }


# ── Prediction ─────────────────────────────────────────────────────────────
def predict_direction(
    candles: list,
    symbol: str,
    sentiment_scores: dict | None = None,
    *,
    interval: str = DEFAULT_INTERVAL,
) -> dict:
    """Probability of an up move per horizon.

    ``interval`` must match what the registered model was trained on. Passing
    the wrong one raises ``FeatureContractMismatch`` rather than silently
    returning confident nonsense — that failure mode is the whole reason this
    argument exists and is required to be correct.

    ``sentiment_scores`` is accepted for API compatibility; sentiment now
    reaches the model through the stored FinBERT archive
    (``services/features/sentiment.py``) rather than through this argument,
    because a value passed per call cannot be reproduced at training time.
    """
    if not candles:
        return {"error": "No candle data supplied", "symbol": symbol}

    cache_key = _cache_key(symbol, candles, interval)
    cached = _cache_get(cache_key)
    if cached:
        return cached

    meta, bundles = _load_registry()

    if meta is not None:
        # The guard that the old pipeline could not produce.
        validate_interval(meta.interval, interval, model_name=f"model {meta.version}")

    row, context, info = _build_features(candles, symbol)
    if row is None:
        return {"error": info.get("reason", "insufficient data for prediction"),
                "symbol": symbol}

    features = {f: _safe_float(row.get(f)) for f in FEATURE_NAMES}

    if bundles:
        result = _predict_with_models(row, features, meta, bundles, symbol)
    else:
        result = _predict_heuristic(features, symbol)

    result.update({
        "symbol": symbol,
        "interval": interval,
        "feature_context": context,
        "feature_set_version": FEATURE_SET_VERSION,
        "generated_at": datetime.now().isoformat(),
        "as_of": info.get("as_of"),
    })
    if info.get("note"):
        result["feature_note"] = info["note"]

    _cache_set(cache_key, result)
    return result


def _predict_with_models(
    row: pd.Series,
    features: dict,
    meta: Any,
    bundles: dict,
    symbol: str,
) -> dict:
    X = feature_matrix(pd.DataFrame([row]))
    predictions: dict[str, dict] = {}

    for horizon in TIMEFRAMES:
        bundle = bundles.get(horizon)
        if not bundle:
            continue
        try:
            raw_p = float(bundle["model"].predict_proba(X)[0][1])
            calibrator = bundle.get("calibrator")
            p = float(calibrator.predict([raw_p])[0]) if calibrator is not None else raw_p
            p = min(max(p, 0.001), 0.999)
        except Exception as e:
            logger.warning("prediction: %s/%s failed (%s)", symbol, horizon, e)
            continue

        predictions[horizon] = {
            "direction": "up" if p > 0.52 else "down" if p < 0.48 else "neutral",
            "probability_up": round(p, 4),
            "confidence": round(max(p, 1.0 - p), 4),
            "score": round(p - 0.5, 4),
            "calibrated": bundle.get("calibrator") is not None,
        }

    if not predictions:
        return _predict_heuristic(features, symbol)

    bullish, bearish = _shap_drivers(X, bundles, features)
    return {
        "predictions": predictions,
        "model_type": meta.estimator,
        "model_version": meta.version,
        "calibrated": meta.calibrated,
        "top_bullish_signals": bullish,
        "top_bearish_signals": bearish,
        "features": {k: round(v, 4) for k, v in features.items() if v is not None},
        **_overall(predictions),
    }


def _shap_drivers(X: np.ndarray, bundles: dict, features: dict) -> tuple[list, list]:
    """Which features pushed this prediction, by SHAP contribution.

    The previous implementation sorted features by absolute magnitude, which
    ranks whichever feature happens to be on the largest scale rather than
    whichever mattered. SHAP answers the question that was actually being asked.
    """
    bundle = bundles.get("1week") or next(iter(bundles.values()), None)
    if bundle is None:
        return [], []

    try:
        import shap

        explainer = shap.TreeExplainer(bundle["model"])
        values = explainer.shap_values(np.nan_to_num(X, nan=0.0))
        if isinstance(values, list):
            values = values[-1]
        contributions = np.asarray(values).reshape(-1)
        if len(contributions) != len(FEATURE_NAMES):
            raise ValueError("shap output does not match the feature contract")
    except Exception as e:
        logger.debug("prediction: SHAP unavailable (%s); falling back to magnitude", e)
        return _magnitude_drivers(features)

    order = np.argsort(contributions)
    bearish = [
        {"feature": _label(FEATURE_NAMES[i]),
         "value": _round(features.get(FEATURE_NAMES[i])),
         "contribution": round(float(contributions[i]), 4)}
        for i in order[:3] if contributions[i] < 0
    ]
    bullish = [
        {"feature": _label(FEATURE_NAMES[i]),
         "value": _round(features.get(FEATURE_NAMES[i])),
         "contribution": round(float(contributions[i]), 4)}
        for i in order[::-1][:3] if contributions[i] > 0
    ]
    return bullish, bearish


def _magnitude_drivers(features: dict) -> tuple[list, list]:
    ranked = sorted(
        ((k, v) for k, v in features.items() if v is not None and k not in _NON_SIGNAL),
        key=lambda kv: abs(kv[1]), reverse=True,
    )
    bullish = [{"feature": _label(k), "value": _round(v)} for k, v in ranked if v > 0][:3]
    bearish = [{"feature": _label(k), "value": _round(v)} for k, v in ranked if v < 0][:3]
    return bullish, bearish


# ── Heuristic fallback ─────────────────────────────────────────────────────
def _predict_heuristic(features: dict, symbol: str) -> dict:
    """A weighted technical score, used when no model is registered.

    This is reasonable technical analysis and it is not machine learning. The
    response says so in ``model_type`` and ``disclaimer`` so no caller can
    mistake it for a model prediction, which is exactly what happened before.
    """
    predictions: dict[str, dict] = {}

    for horizon in TIMEFRAMES:
        score = 0.0
        max_score = 0.0

        rsi = features.get("rsi_14")
        if rsi is not None:
            score += 2.0 if rsi < 30 else 1.0 if rsi < 40 else -2.0 if rsi > 70 else -1.0 if rsi > 60 else 0.0
            max_score += 2.0

        slope = features.get("rsi_slope_5")
        if slope is not None:
            score += 1.0 if slope > 5 else -1.0 if slope < -5 else 0.0
            max_score += 1.0

        hist = features.get("macd_histogram")
        if hist is not None:
            score += 1.5 if hist > 0 else -1.5
            max_score += 1.5

        for key, weight in (("sma_20_dist", 0.5), ("sma_50_dist", 0.5)):
            val = features.get(key)
            if val is not None:
                score += weight if val > 0 else -weight
                max_score += weight

        bb = features.get("bb_position")
        if bb is not None:
            score += 1.5 if bb < 0.2 else 0.5 if bb < 0.35 else -1.5 if bb > 0.8 else -0.5 if bb > 0.65 else 0.0
            max_score += 1.5

        stoch = features.get("stoch_k")
        if stoch is not None:
            score += 1.0 if stoch < 20 else -1.0 if stoch > 80 else 0.0
            max_score += 1.0

        # Momentum matters more at a day than at a month.
        momentum_w = {"1day": 1.2, "1week": 0.9, "1month": 0.5}[horizon]
        for key, threshold in (("return_1", 0.005), ("return_5", 0.01)):
            val = features.get(key)
            if val is not None:
                score += (0.5 * momentum_w) if val > threshold else \
                    (-0.5 * momentum_w) if val < -threshold else 0.0
                max_score += 0.5 * momentum_w

        norm = (score / max_score + 1) / 2 if max_score > 0 else 0.5
        confidence = max(0.35, min(0.75, norm if norm > 0.5 else 1 - norm))

        predictions[horizon] = {
            "direction": "up" if score > 0 else "down" if score < 0 else "neutral",
            "probability_up": round(min(max(norm, 0.05), 0.95), 4),
            "confidence": round(confidence, 3),
            "score": round(norm - 0.5, 4),
            "calibrated": False,
        }

    bullish, bearish = _magnitude_drivers(features)
    return {
        "predictions": predictions,
        "model_type": "heuristic",
        "model_version": None,
        "calibrated": False,
        "disclaimer": "No trained model is registered. These are rule-based "
                      "technical scores, not machine-learning predictions, and "
                      "the confidence shown is not a calibrated probability.",
        "top_bullish_signals": bullish,
        "top_bearish_signals": bearish,
        "features": {k: round(v, 4) for k, v in features.items() if v is not None},
        **_overall(predictions),
    }


def _overall(predictions: dict) -> dict:
    scores = [p["score"] for p in predictions.values()]
    mean = float(np.mean(scores)) if scores else 0.0
    return {
        "overall_outlook": "bullish" if mean > 0.02 else "bearish" if mean < -0.02 else "neutral",
        "overall_score": round(mean, 4),
    }


# ── Helpers ────────────────────────────────────────────────────────────────
_NON_SIGNAL = {"dow_sin", "dow_cos", "sent_has_news", "fund_is_stale_days"}

_LABELS = {
    "rsi_14": "RSI (14)", "rsi_slope_5": "RSI momentum",
    "macd_histogram": "MACD histogram", "macd_slope_5": "MACD acceleration",
    "sma_20_dist": "Price vs SMA 20", "sma_50_dist": "Price vs SMA 50",
    "sma_200_dist": "Price vs SMA 200",
    "bb_position": "Bollinger position", "bb_width": "Bollinger width",
    "adx_14": "Trend strength (ADX)", "atr_14_pct": "Volatility (ATR %)",
    "volume_ratio": "Volume vs average", "deliv_pct": "Delivery %",
    "deliv_pct_z20": "Delivery vs its own norm",
    "turnover_rank_uni": "Turnover rank in universe",
    "rel_strength_20": "Strength vs market (20d)",
    "rel_to_industry_20": "Strength vs industry (20d)",
    "mkt_return_20": "Market return (20d)",
    "mkt_breadth_adv_pct": "Market breadth",
    "fund_score_total": "Fundamental score",
    "sent_net_7d": "News sentiment (7d)",
}


def _label(name: str) -> str:
    if name in _LABELS:
        return _LABELS[name]
    if name.startswith("xs_uni_"):
        return f"{_label(name[7:])} — rank in universe"
    if name.startswith("xs_ind_"):
        return f"{_label(name[7:])} — rank in industry"
    return name.replace("_", " ").capitalize()


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else f


def _round(val) -> Optional[float]:
    f = _safe_float(val)
    return round(f, 4) if f is not None else None


def _norm(symbol: str) -> str:
    s = str(symbol).strip().upper()
    for suffix in ("-EQ", "-BE", ".NS"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s
