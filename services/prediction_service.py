"""
LightGBM-based price direction prediction service.

Predicts whether a stock's price will go UP or DOWN across 7 timeframes:
10 min, 1 hr, 4 hr, 1 day, 1 week, 1 month, 1 year.

Uses technical indicators (RSI, MACD, Bollinger Bands, SMAs, volume),
price-action features (returns, volatility, candle patterns), and
optional sentiment scores as input features.
"""

import os
import time
import logging
import hashlib
from typing import Optional
from datetime import datetime

import numpy as np
import pandas as pd
from ta.trend import SMAIndicator, MACD, EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange

logger = logging.getLogger(__name__)

TIMEFRAMES = ["10min", "1hr", "4hr", "1day", "1week", "1month", "1year"]
TIMEFRAME_BARS = {"10min": 2, "1hr": 12, "4hr": 48, "1day": 1, "1week": 5, "1month": 22, "1year": 252}

_models: dict = {}
_MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
_cache: dict[str, dict] = {}
_CACHE_TTL = 120


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: str, data):
    _cache[key] = {"data": data, "ts": time.time()}


def _load_models():
    """Lazy-load trained LightGBM models from disk."""
    global _models
    if _models:
        return _models

    try:
        import joblib
    except ImportError:
        logger.warning("joblib not installed — predictions unavailable")
        return {}

    for tf in TIMEFRAMES:
        path = os.path.join(_MODEL_DIR, f"lgbm_{tf}.pkl")
        if os.path.exists(path):
            try:
                _models[tf] = joblib.load(path)
                logger.info("Loaded prediction model for %s", tf)
            except Exception as e:
                logger.warning("Failed to load model %s: %s", tf, e)

    if not _models:
        logger.info("No trained models found — will use heuristic predictions")
    return _models


FEATURE_NAMES = [
    "rsi_14", "rsi_slope_5",
    "macd", "macd_signal", "macd_histogram", "macd_slope_5",
    "sma_20_dist", "sma_50_dist", "sma_200_dist",
    "ema_9_dist", "ema_21_dist",
    "bb_position", "bb_width",
    "adx_14",
    "stoch_k", "stoch_d",
    "atr_14_pct",
    "volume_ratio", "volume_trend",
    "return_1", "return_3", "return_5", "return_10", "return_20",
    "volatility_5", "volatility_10", "volatility_20",
    "candle_body_ratio", "upper_shadow", "lower_shadow",
    "high_low_range",
    "hour_sin", "hour_cos",
    "dow_sin", "dow_cos",
]


def extract_features(candles: list, sentiment_scores: dict | None = None) -> Optional[dict]:
    """
    Extract ML features from OHLCV candle data.
    candles: list of [timestamp, open, high, low, close, volume]
    Returns a dict of feature_name -> value, or None if insufficient data.
    """
    if not candles or len(candles) < 50:
        return None

    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["close"], inplace=True)

    if len(df) < 50:
        return None

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    price = float(close.iloc[-1])

    features = {}

    rsi = RSIIndicator(close, window=14).rsi()
    rsi_val = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50.0
    features["rsi_14"] = rsi_val
    rsi_5ago = float(rsi.iloc[-6]) if len(rsi) >= 6 and pd.notna(rsi.iloc[-6]) else rsi_val
    features["rsi_slope_5"] = rsi_val - rsi_5ago

    macd_ind = MACD(close)
    macd_line = macd_ind.macd()
    macd_signal = macd_ind.macd_signal()
    macd_hist = macd_ind.macd_diff()
    features["macd"] = _safe_last(macd_line, 0.0)
    features["macd_signal"] = _safe_last(macd_signal, 0.0)
    features["macd_histogram"] = _safe_last(macd_hist, 0.0)
    hist_5ago = float(macd_hist.iloc[-6]) if len(macd_hist) >= 6 and pd.notna(macd_hist.iloc[-6]) else 0.0
    features["macd_slope_5"] = features["macd_histogram"] - hist_5ago

    sma_20 = SMAIndicator(close, window=20).sma_indicator()
    sma_50 = SMAIndicator(close, window=50).sma_indicator()
    features["sma_20_dist"] = (price - _safe_last(sma_20, price)) / max(price, 1e-9)
    features["sma_50_dist"] = (price - _safe_last(sma_50, price)) / max(price, 1e-9)

    if len(close) >= 200:
        sma_200 = SMAIndicator(close, window=200).sma_indicator()
        features["sma_200_dist"] = (price - _safe_last(sma_200, price)) / max(price, 1e-9)
    else:
        features["sma_200_dist"] = 0.0

    ema_9 = EMAIndicator(close, window=9).ema_indicator()
    ema_21 = EMAIndicator(close, window=21).ema_indicator()
    features["ema_9_dist"] = (price - _safe_last(ema_9, price)) / max(price, 1e-9)
    features["ema_21_dist"] = (price - _safe_last(ema_21, price)) / max(price, 1e-9)

    bb = BollingerBands(close, window=20)
    bb_upper = _safe_last(bb.bollinger_hband(), price)
    bb_lower = _safe_last(bb.bollinger_lband(), price)
    bb_range = bb_upper - bb_lower
    features["bb_position"] = (price - bb_lower) / max(bb_range, 1e-9)
    features["bb_width"] = bb_range / max(price, 1e-9)

    if len(close) >= 14:
        adx = ADXIndicator(high, low, close, window=14)
        features["adx_14"] = _safe_last(adx.adx(), 25.0)
    else:
        features["adx_14"] = 25.0

    if len(close) >= 14:
        stoch = StochasticOscillator(high, low, close, window=14, smooth_window=3)
        features["stoch_k"] = _safe_last(stoch.stoch(), 50.0)
        features["stoch_d"] = _safe_last(stoch.stoch_signal(), 50.0)
    else:
        features["stoch_k"] = 50.0
        features["stoch_d"] = 50.0

    atr = AverageTrueRange(high, low, close, window=14)
    atr_val = _safe_last(atr.average_true_range(), 0.0)
    features["atr_14_pct"] = atr_val / max(price, 1e-9)

    vol_sma = SMAIndicator(volume, window=20).sma_indicator()
    avg_vol = _safe_last(vol_sma, 1.0)
    curr_vol = float(volume.iloc[-1]) if pd.notna(volume.iloc[-1]) else 0.0
    features["volume_ratio"] = curr_vol / max(avg_vol, 1.0)
    vol_5 = volume.tail(5).mean()
    vol_20 = volume.tail(20).mean()
    features["volume_trend"] = float(vol_5 / max(vol_20, 1.0))

    returns = close.pct_change()
    for n in [1, 3, 5, 10, 20]:
        if len(returns) >= n:
            features[f"return_{n}"] = float(returns.iloc[-1:].sum()) if n == 1 else float(
                (close.iloc[-1] / close.iloc[-n] - 1) if close.iloc[-n] != 0 else 0
            )
        else:
            features[f"return_{n}"] = 0.0

    for n in [5, 10, 20]:
        if len(returns) >= n:
            features[f"volatility_{n}"] = float(returns.tail(n).std())
        else:
            features[f"volatility_{n}"] = 0.0

    o, h_val, l_val, c = (
        float(df["open"].iloc[-1]),
        float(df["high"].iloc[-1]),
        float(df["low"].iloc[-1]),
        float(df["close"].iloc[-1]),
    )
    hl_range = h_val - l_val
    features["candle_body_ratio"] = abs(c - o) / max(hl_range, 1e-9)
    features["upper_shadow"] = (h_val - max(o, c)) / max(hl_range, 1e-9)
    features["lower_shadow"] = (min(o, c) - l_val) / max(hl_range, 1e-9)
    features["high_low_range"] = hl_range / max(price, 1e-9)

    try:
        ts = pd.to_datetime(df["timestamp"].iloc[-1])
        hour = ts.hour + ts.minute / 60
        dow = ts.dayofweek
    except Exception:
        hour, dow = 12.0, 2
    features["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    features["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    features["dow_sin"] = np.sin(2 * np.pi * dow / 5)
    features["dow_cos"] = np.cos(2 * np.pi * dow / 5)

    for k, v in features.items():
        if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
            features[k] = 0.0

    return features


def _safe_last(series: pd.Series, default: float) -> float:
    if series is None or series.empty:
        return default
    val = series.iloc[-1]
    return float(val) if pd.notna(val) else default


def _heuristic_predict(features: dict) -> dict:
    """
    Rule-based prediction when no trained model is available.
    Uses a weighted scoring system across technical indicators.
    """
    predictions = {}

    for tf in TIMEFRAMES:
        score = 0.0
        max_score = 0.0

        rsi = features.get("rsi_14", 50)
        if rsi < 30:
            score += 2.0
        elif rsi < 40:
            score += 1.0
        elif rsi > 70:
            score -= 2.0
        elif rsi > 60:
            score -= 1.0
        max_score += 2.0

        rsi_slope = features.get("rsi_slope_5", 0)
        if rsi_slope > 5:
            score += 1.0
        elif rsi_slope < -5:
            score -= 1.0
        max_score += 1.0

        macd_hist = features.get("macd_histogram", 0)
        if macd_hist > 0:
            score += 1.5
        else:
            score -= 1.5
        max_score += 1.5

        macd_slope = features.get("macd_slope_5", 0)
        if macd_slope > 0:
            score += 1.0
        elif macd_slope < 0:
            score -= 1.0
        max_score += 1.0

        sma_20_dist = features.get("sma_20_dist", 0)
        sma_50_dist = features.get("sma_50_dist", 0)
        if sma_20_dist > 0:
            score += 0.5
        else:
            score -= 0.5
        if sma_50_dist > 0:
            score += 0.5
        else:
            score -= 0.5
        max_score += 1.0

        bb_pos = features.get("bb_position", 0.5)
        if bb_pos < 0.2:
            score += 1.5
        elif bb_pos < 0.35:
            score += 0.5
        elif bb_pos > 0.8:
            score -= 1.5
        elif bb_pos > 0.65:
            score -= 0.5
        max_score += 1.5

        adx = features.get("adx_14", 25)
        vol_ratio = features.get("volume_ratio", 1.0)
        trend_str = 1.0 + (0.3 if adx > 25 else -0.1)
        if vol_ratio > 1.5:
            trend_str += 0.2
        max_score += 0.5

        stoch_k = features.get("stoch_k", 50)
        if stoch_k < 20:
            score += 1.0
        elif stoch_k > 80:
            score -= 1.0
        max_score += 1.0

        if tf in ("10min", "1hr"):
            momentum_w = 1.3
        elif tf in ("4hr",):
            momentum_w = 1.0
        elif tf in ("1day", "1week"):
            momentum_w = 0.7
        else:
            momentum_w = 0.4

        ret_1 = features.get("return_1", 0)
        ret_5 = features.get("return_5", 0)
        if ret_1 > 0.005:
            score += 0.5 * momentum_w
        elif ret_1 < -0.005:
            score -= 0.5 * momentum_w
        if ret_5 > 0.01:
            score += 0.5 * momentum_w
        elif ret_5 < -0.01:
            score -= 0.5 * momentum_w
        max_score += 1.0 * momentum_w

        score *= trend_str

        raw_confidence = (score / max(max_score * trend_str, 1e-9) + 1) / 2
        confidence = max(0.35, min(0.85, raw_confidence))

        direction = "up" if score > 0 else "down" if score < 0 else "neutral"

        predictions[tf] = {
            "direction": direction,
            "confidence": round(confidence, 3),
            "score": round(score, 3),
        }

    return predictions


def predict_direction(
    candles: list,
    symbol: str,
    sentiment_scores: dict | None = None,
) -> dict:
    """
    Main prediction entry point.
    Returns predictions for all 5 timeframes with direction and confidence.
    """
    cache_key = f"pred:{symbol}:{hashlib.md5(str(len(candles)).encode()).hexdigest()}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    features = extract_features(candles, sentiment_scores)
    if features is None:
        return {
            "error": "Insufficient data for prediction (need 50+ candles)",
            "symbol": symbol,
        }

    models = _load_models()
    predictions = {}

    if models:
        feature_array = np.array([[features.get(f, 0.0) for f in FEATURE_NAMES]])
        for tf in TIMEFRAMES:
            model = models.get(tf)
            if model:
                try:
                    proba = model.predict_proba(feature_array)[0]
                    up_prob = float(proba[1]) if len(proba) > 1 else float(proba[0])
                    direction = "up" if up_prob > 0.52 else "down" if up_prob < 0.48 else "neutral"
                    predictions[tf] = {
                        "direction": direction,
                        "confidence": round(max(up_prob, 1 - up_prob), 3),
                        "score": round(up_prob - 0.5, 3),
                    }
                except Exception as e:
                    logger.warning("Model prediction failed for %s/%s: %s", symbol, tf, e)
                    predictions[tf] = _heuristic_predict(features).get(tf)
            else:
                predictions[tf] = _heuristic_predict(features).get(tf)
    else:
        predictions = _heuristic_predict(features)

    top_bullish = []
    top_bearish = []
    for name, val in sorted(features.items(), key=lambda x: abs(x[1]), reverse=True):
        if name in ("hour_sin", "hour_cos", "dow_sin", "dow_cos"):
            continue
        if len(top_bullish) < 3 and val > 0:
            top_bullish.append({"feature": _feature_label(name), "value": round(val, 4)})
        if len(top_bearish) < 3 and val < 0:
            top_bearish.append({"feature": _feature_label(name), "value": round(val, 4)})
        if len(top_bullish) >= 3 and len(top_bearish) >= 3:
            break

    overall_score = sum(p["score"] for p in predictions.values()) / len(predictions)
    if overall_score > 0.03:
        overall = "bullish"
    elif overall_score < -0.03:
        overall = "bearish"
    else:
        overall = "neutral"

    result = {
        "symbol": symbol,
        "predictions": predictions,
        "overall_outlook": overall,
        "overall_score": round(overall_score, 4),
        "model_type": "lightgbm" if models else "heuristic",
        "top_bullish_signals": top_bullish,
        "top_bearish_signals": top_bearish,
        "features": {k: round(v, 4) for k, v in features.items()},
        "generated_at": datetime.now().isoformat(),
    }

    _cache_set(cache_key, result)
    return result


def _feature_label(name: str) -> str:
    labels = {
        "rsi_14": "RSI (14)",
        "rsi_slope_5": "RSI Momentum",
        "macd": "MACD Line",
        "macd_signal": "MACD Signal",
        "macd_histogram": "MACD Histogram",
        "macd_slope_5": "MACD Acceleration",
        "sma_20_dist": "Price vs SMA 20",
        "sma_50_dist": "Price vs SMA 50",
        "sma_200_dist": "Price vs SMA 200",
        "ema_9_dist": "Price vs EMA 9",
        "ema_21_dist": "Price vs EMA 21",
        "bb_position": "Bollinger Position",
        "bb_width": "Bollinger Width",
        "adx_14": "Trend Strength (ADX)",
        "stoch_k": "Stochastic %K",
        "stoch_d": "Stochastic %D",
        "atr_14_pct": "Volatility (ATR%)",
        "volume_ratio": "Volume vs Avg",
        "volume_trend": "Volume Trend",
        "return_1": "1-Bar Return",
        "return_3": "3-Bar Return",
        "return_5": "5-Bar Return",
        "return_10": "10-Bar Return",
        "return_20": "20-Bar Return",
        "volatility_5": "5-Bar Volatility",
        "volatility_10": "10-Bar Volatility",
        "volatility_20": "20-Bar Volatility",
        "candle_body_ratio": "Candle Body Size",
        "upper_shadow": "Upper Shadow",
        "lower_shadow": "Lower Shadow",
        "high_low_range": "High-Low Range",
    }
    return labels.get(name, name.replace("_", " ").title())
