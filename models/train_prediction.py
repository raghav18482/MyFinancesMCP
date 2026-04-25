#!/usr/bin/env python3
"""
Offline training script for LightGBM price direction models.

Usage:
    python models/train_prediction.py

This script:
1. Connects to Angel One via env credentials
2. Fetches historical candle data for NIFTY 50 / top NSE stocks
3. Engineers features + labels for 7 timeframes
4. Trains LightGBM binary classifiers
5. Saves models to models/lgbm_*.pkl

Requires: ANGELONE_API_KEY, ANGELONE_CLIENT_ID, ANGELONE_PASSWORD, ANGELONE_TOTP_SECRET in .env
"""

import os
import sys
import time
import logging

_project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_project_dir)
sys.path.insert(0, _project_dir)

import numpy as np
import pandas as pd
import joblib
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from services.prediction_service import extract_features, FEATURE_NAMES, TIMEFRAMES, TIMEFRAME_BARS

NIFTY50_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN",
    "SUNPHARMA", "BAJFINANCE", "WIPRO", "ULTRACEMCO", "HCLTECH",
    "NTPC", "TATAMOTORS", "POWERGRID", "M&M", "ADANIENT",
    "TATASTEEL", "BAJAJFINSV", "NESTLEIND", "JSWSTEEL", "TECHM",
    "INDUSINDBK", "ONGC", "GRASIM", "COALINDIA", "CIPLA",
    "BPCL", "DRREDDY", "APOLLOHOSP", "EICHERMOT", "TATACONSUM",
    "DIVISLAB", "SBILIFE", "HEROMOTOCO", "BRITANNIA", "HINDALCO",
    "BAJAJ-AUTO", "ADANIPORTS", "LTIM", "HDFCLIFE", "SHRIRAMFIN",
]

MODEL_DIR = os.path.join(_project_dir, "models")


def _connect_angel():
    from angel_client import AngelOneClient
    api_key = os.environ.get("ANGELONE_API_KEY") or os.environ.get("ANGEL_API_KEY")
    client_id = os.environ.get("ANGELONE_CLIENT_ID") or os.environ.get("ANGEL_CLIENT_ID")
    password = os.environ.get("ANGELONE_PASSWORD") or os.environ.get("ANGEL_PASSWORD")
    totp_secret = os.environ.get("ANGELONE_TOTP_SECRET") or os.environ.get("ANGEL_TOTP_SECRET")

    if not all([api_key, client_id, password, totp_secret]):
        raise RuntimeError("Missing Angel One credentials in .env")

    client = AngelOneClient(api_key, client_id, password, totp_secret)
    client.ensure_session()
    logger.info("Connected to Angel One as %s", client_id)
    return client


def _fetch_candles(client, symbol, exchange="NSE", interval="FIVE_MINUTE", days=60):
    """Fetch candle data for a symbol."""
    from datetime import datetime, timedelta

    try:
        client.ensure_session()
        sr = client.search_scrip(exchange, symbol)
        if not sr:
            return None

        status = sr.get("status") or sr.get("success")
        rows = sr.get("data") or []
        if not status or not rows:
            return None

        if isinstance(rows, str):
            return None

        token = None
        for r in rows:
            ts = r.get("tradingsymbol", "")
            if ts == symbol or ts == f"{symbol}-EQ":
                token = r.get("symboltoken")
                break
        if not token and rows:
            token = rows[0].get("symboltoken")
        if not token:
            return None

        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=days)
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_dt.strftime("%Y-%m-%d 09:15"),
            "todate": to_dt.strftime("%Y-%m-%d 15:30"),
        }
        result = client.get_candle_data(params)
        if result and (result.get("status") or result.get("success")) and result.get("data"):
            return result["data"]
    except Exception as e:
        logger.warning("Failed to fetch %s: %s", symbol, e)
    return None


def _build_labeled_dataset(candles: list, symbol: str) -> list[dict]:
    """Build feature+label rows from candle data using sliding window."""
    if not candles or len(candles) < 100:
        return []

    rows = []
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    max_forward = max(TIMEFRAME_BARS.values())

    for i in range(50, len(df) - max_forward):
        window = candles[:i + 1]
        features = extract_features(window)
        if features is None:
            continue

        current_close = float(df["close"].iloc[i])
        labels = {}
        for tf, bars in TIMEFRAME_BARS.items():
            future_idx = min(i + bars, len(df) - 1)
            future_close = float(df["close"].iloc[future_idx])
            labels[f"label_{tf}"] = 1 if future_close > current_close else 0

        row = {**features, **labels, "symbol": symbol}
        rows.append(row)

    return rows


def train():
    logger.info("=== LightGBM Prediction Model Training ===")

    try:
        import lightgbm as lgb
    except ImportError:
        logger.error("lightgbm not installed. Run: pip install lightgbm")
        sys.exit(1)

    try:
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score, classification_report
    except ImportError:
        logger.error("scikit-learn not installed. Run: pip install scikit-learn")
        sys.exit(1)

    client = _connect_angel()

    all_rows = []
    symbols_to_train = NIFTY50_SYMBOLS[:20]

    for idx, symbol in enumerate(symbols_to_train):
        logger.info("[%d/%d] Fetching %s...", idx + 1, len(symbols_to_train), symbol)
        candles = _fetch_candles(client, symbol, interval="FIVE_MINUTE", days=30)

        if not candles:
            logger.warning("  No data for %s, trying ONE_DAY...", symbol)
            candles = _fetch_candles(client, symbol, interval="ONE_DAY", days=365)

        if not candles:
            logger.warning("  Skipping %s — no data", symbol)
            continue

        logger.info("  Got %d candles for %s", len(candles), symbol)
        rows = _build_labeled_dataset(candles, symbol)
        logger.info("  Generated %d labeled rows", len(rows))
        all_rows.extend(rows)

        time.sleep(0.5)

    if len(all_rows) < 100:
        logger.error("Not enough training data (%d rows). Need at least 100.", len(all_rows))
        sys.exit(1)

    logger.info("Total training samples: %d", len(all_rows))
    dataset = pd.DataFrame(all_rows)

    os.makedirs(MODEL_DIR, exist_ok=True)

    for tf in TIMEFRAMES:
        label_col = f"label_{tf}"
        if label_col not in dataset.columns:
            logger.warning("No label column %s — skipping", label_col)
            continue

        X = dataset[FEATURE_NAMES].values
        y = dataset[label_col].values

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42, shuffle=True
        )

        model = lgb.LGBMClassifier(
            n_estimators=300,
            max_depth=6,
            learning_rate=0.05,
            num_leaves=31,
            min_child_samples=20,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,
            reg_lambda=0.1,
            random_state=42,
            verbosity=-1,
        )

        model.fit(
            X_train, y_train,
            eval_set=[(X_test, y_test)],
            callbacks=[lgb.log_evaluation(50)],
        )

        y_pred = model.predict(X_test)
        acc = accuracy_score(y_test, y_pred)
        logger.info("\n=== %s Model ===", tf)
        logger.info("Accuracy: %.4f", acc)
        logger.info("\n%s", classification_report(y_test, y_pred, target_names=["DOWN", "UP"]))

        importance = sorted(
            zip(FEATURE_NAMES, model.feature_importances_),
            key=lambda x: x[1],
            reverse=True,
        )
        logger.info("Top 10 features:")
        for feat, imp in importance[:10]:
            logger.info("  %s: %d", feat, imp)

        model_path = os.path.join(MODEL_DIR, f"lgbm_{tf}.pkl")
        joblib.dump(model, model_path)
        logger.info("Saved model → %s", model_path)

    logger.info("\n=== Training complete! ===")
    logger.info("Models saved to %s/", MODEL_DIR)


if __name__ == "__main__":
    train()
