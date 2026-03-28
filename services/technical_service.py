import time
import logging
from typing import Optional
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
from ta.trend import SMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands

logger = logging.getLogger(__name__)

_cache: dict[str, dict] = {}
_CACHE_TTL = 3600  # 1 hour during market, refreshes naturally


def _cache_get(key: str) -> Optional[dict]:
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: str, data: dict):
    _cache[key] = {"data": data, "ts": time.time()}


def compute_technical_indicators(
    candles: list,
    trading_symbol: str,
    avg_price: float | None = None,
) -> dict:
    """Compute all technical indicators from OHLCV candle data.

    Args:
        candles: List of [timestamp, open, high, low, close, volume]
        trading_symbol: The stock symbol for display
        avg_price: User's average buy price (optional, for chart overlay)

    Returns dict with indicators, signals, support/resistance, and chart data.
    """
    cache_key = f"technical:{trading_symbol}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    if not candles or len(candles) < 20:
        return {"error": "Not enough candle data (need at least 20 days)", "symbol": trading_symbol}

    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["timestamp"]).dt.strftime("%Y-%m-%d")

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    current_price = float(close.iloc[-1])
    result = {
        "symbol": trading_symbol,
        "current_price": current_price,
        "avg_price": avg_price,
        "error": None,
    }

    # Moving Averages
    sma_50 = SMAIndicator(close, window=50).sma_indicator() if len(close) >= 50 else pd.Series(dtype=float)
    sma_200 = SMAIndicator(close, window=200).sma_indicator() if len(close) >= 200 else pd.Series(dtype=float)
    sma_20 = SMAIndicator(close, window=20).sma_indicator()

    result["indicators"] = {
        "sma_20": _last_valid(sma_20),
        "sma_50": _last_valid(sma_50),
        "sma_200": _last_valid(sma_200),
    }

    # RSI
    rsi = RSIIndicator(close, window=14).rsi()
    rsi_val = _last_valid(rsi)
    result["indicators"]["rsi"] = rsi_val

    # MACD
    macd_ind = MACD(close)
    macd_line = macd_ind.macd()
    macd_signal = macd_ind.macd_signal()
    macd_hist = macd_ind.macd_diff()
    result["indicators"]["macd"] = _last_valid(macd_line)
    result["indicators"]["macd_signal"] = _last_valid(macd_signal)
    result["indicators"]["macd_histogram"] = _last_valid(macd_hist)

    # Bollinger Bands
    bb = BollingerBands(close, window=20)
    result["indicators"]["bb_upper"] = _last_valid(bb.bollinger_hband())
    result["indicators"]["bb_lower"] = _last_valid(bb.bollinger_lband())

    # Volume analysis
    vol_sma_20 = SMAIndicator(volume, window=20).sma_indicator()
    current_vol = float(volume.iloc[-1])
    avg_vol = _last_valid(vol_sma_20) or 1
    result["indicators"]["volume_current"] = current_vol
    result["indicators"]["volume_avg_20"] = avg_vol
    result["indicators"]["volume_ratio"] = round(current_vol / avg_vol, 2) if avg_vol else None

    # 52-week high/low (use available data)
    last_252 = close.tail(min(252, len(close)))
    high_252 = high.tail(min(252, len(high)))
    low_252 = low.tail(min(252, len(low)))
    week52_high = float(high_252.max())
    week52_low = float(low_252.min())
    result["indicators"]["week52_high"] = week52_high
    result["indicators"]["week52_low"] = week52_low
    if week52_high > week52_low:
        result["indicators"]["week52_position"] = round(
            (current_price - week52_low) / (week52_high - week52_low) * 100, 1
        )
    else:
        result["indicators"]["week52_position"] = 50.0

    # Support & Resistance via pivot points (classic)
    last_high = float(high.iloc[-1])
    last_low = float(low.iloc[-1])
    last_close = current_price
    pivot = round((last_high + last_low + last_close) / 3, 2)
    result["support_resistance"] = {
        "pivot": pivot,
        "r1": round(2 * pivot - last_low, 2),
        "r2": round(pivot + (last_high - last_low), 2),
        "s1": round(2 * pivot - last_high, 2),
        "s2": round(pivot - (last_high - last_low), 2),
    }

    # Signal summary
    result["signals"] = _generate_signals(result["indicators"], current_price)

    # Chart-ready arrays (for frontend ECharts)
    result["chart_data"] = {
        "dates": df["date"].tolist(),
        "ohlc": df[["open", "high", "low", "close"]].values.tolist(),
        "volume": volume.tolist(),
        "sma_50": _series_to_list(sma_50),
        "sma_200": _series_to_list(sma_200),
        "rsi": _series_to_list(rsi),
        "macd_line": _series_to_list(macd_line),
        "macd_signal": _series_to_list(macd_signal),
        "macd_histogram": _series_to_list(macd_hist),
    }

    _cache_set(cache_key, result)
    return result


def _last_valid(series: pd.Series) -> Optional[float]:
    if series is None or series.empty:
        return None
    last = series.dropna()
    if last.empty:
        return None
    return round(float(last.iloc[-1]), 2)


def _series_to_list(series: pd.Series) -> list:
    if series is None or series.empty:
        return []
    return [round(float(v), 2) if pd.notna(v) else None for v in series]


def _generate_signals(indicators: dict, price: float) -> list[dict]:
    """Generate human-readable signal summaries."""
    signals = []

    # DMA position
    sma_200 = indicators.get("sma_200")
    if sma_200:
        above = price > sma_200
        signals.append({
            "name": "200 DMA",
            "value": f"₹{sma_200:,.2f}",
            "status": "bullish" if above else "bearish",
            "label": "Above 200 DMA" if above else "Below 200 DMA",
        })

    sma_50 = indicators.get("sma_50")
    if sma_50:
        above = price > sma_50
        signals.append({
            "name": "50 DMA",
            "value": f"₹{sma_50:,.2f}",
            "status": "bullish" if above else "bearish",
            "label": "Above 50 DMA" if above else "Below 50 DMA",
        })

    # RSI
    rsi_val = indicators.get("rsi")
    if rsi_val is not None:
        if rsi_val > 70:
            status, label = "bearish", "Overbought"
        elif rsi_val < 30:
            status, label = "bullish", "Oversold"
        else:
            status, label = "neutral", "Neutral"
        signals.append({
            "name": "RSI (14)",
            "value": str(rsi_val),
            "status": status,
            "label": label,
        })

    # MACD
    macd_val = indicators.get("macd")
    macd_sig = indicators.get("macd_signal")
    if macd_val is not None and macd_sig is not None:
        bullish = macd_val > macd_sig
        signals.append({
            "name": "MACD",
            "value": f"{macd_val:+.2f}",
            "status": "bullish" if bullish else "bearish",
            "label": "Bullish Crossover" if bullish else "Bearish Crossover",
        })

    # Volume
    vol_ratio = indicators.get("volume_ratio")
    if vol_ratio is not None:
        if vol_ratio > 1.5:
            status, label = "bullish", "High Volume"
        elif vol_ratio < 0.5:
            status, label = "bearish", "Low Volume"
        else:
            status, label = "neutral", "Normal Volume"
        signals.append({
            "name": "Volume",
            "value": f"{vol_ratio:.1f}x avg",
            "status": status,
            "label": label,
        })

    # 52-week position
    pos = indicators.get("week52_position")
    if pos is not None:
        if pos > 80:
            status, label = "bearish", "Near 52W High"
        elif pos < 20:
            status, label = "bullish", "Near 52W Low"
        else:
            status, label = "neutral", f"{pos:.0f}% of range"
        signals.append({
            "name": "52W Range",
            "value": f"{pos:.0f}%",
            "status": status,
            "label": label,
        })

    return signals
