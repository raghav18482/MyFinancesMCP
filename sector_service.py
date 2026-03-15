import os
import json
import time
import logging
from typing import Optional
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

logger = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))

_cache: dict[str, dict] = {}
_CACHE_TTL_SECTOR = 6 * 3600   # 6 hours

SECTOR_INDEX_MAP = {
    "Banking": "Nifty Bank",
    "IT": "Nifty IT",
    "Pharma": "Nifty Pharma",
    "FMCG": "Nifty FMCG",
    "Energy": "Nifty Energy",
    "Financial Services": "Nifty Financial Services",
    "Automobile": "Nifty Auto",
    "Metals": "Nifty Metal",
    "Infrastructure": "Nifty Infrastructure",
    "Real Estate": "Nifty Realty",
    "Consumer Durables": "Nifty Consumer Durables",
    "Healthcare": "Nifty Healthcare Index",
    "PSU": "Nifty PSU Bank",
    "Defence": "Nifty India Defence",
}

try:
    with open(os.path.join(_dir, "data", "sector_cycles.json")) as f:
        SECTOR_CYCLES: dict[str, str] = json.load(f)
except Exception:
    SECTOR_CYCLES = {}


def _is_market_hours() -> bool:
    now = datetime.now()
    weekday = now.weekday()
    if weekday >= 5:
        return False
    hour_min = now.hour * 100 + now.minute
    return 915 <= hour_min <= 1545


def _breadth_ttl() -> int:
    return 300 if _is_market_hours() else 3600


def _cache_get(key: str, ttl: int) -> Optional[dict]:
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry["data"]
    return None


def _cache_set(key: str, data: dict):
    _cache[key] = {"data": data, "ts": time.time()}


# ── Individual breadth fetchers (each runs in its own thread) ─────────

def _fetch_heatmap() -> list[dict]:
    from nselib import capital_market
    perf = capital_market.market_watch_all_indices()
    if perf is None or perf.empty:
        return []
    heatmap = []
    for _, row in perf.iterrows():
        name = row.get("index", "")
        change = row.get("percentChange", None)
        if name and change is not None:
            try:
                change_val = float(str(change).replace(",", "").replace("%", ""))
            except (ValueError, TypeError):
                continue
            heatmap.append({"name": str(name), "change": change_val})
    return sorted(heatmap, key=lambda x: x["change"], reverse=True)


def _fetch_52w_report() -> dict:
    from nselib import capital_market
    today = datetime.now()
    # Use most recent weekday as trade_date
    offset = max(0, today.weekday() - 4)  # Sat=1, Sun=2
    trade_dt = today - timedelta(days=offset)
    trade_date = trade_dt.strftime("%d-%m-%Y")
    report = capital_market.week_52_high_low_report(trade_date=trade_date)
    if report is None or report.empty:
        return {}
    highs = 0
    lows = 0
    cutoff = (today - timedelta(days=7)).strftime("%d-%b-%Y").upper()
    if "52_Week_High_Date" in report.columns:
        dates = report["52_Week_High_Date"].astype(str).str.strip()
        try:
            parsed = pd.to_datetime(dates, format="%d-%b-%Y", errors="coerce")
            highs = int((parsed >= pd.Timestamp(today - timedelta(days=7))).sum())
        except Exception:
            highs = len(report)
    if "52_Week_Low_DT" in report.columns:
        dates = report["52_Week_Low_DT"].astype(str).str.strip()
        try:
            parsed = pd.to_datetime(dates, format="%d-%b-%Y", errors="coerce")
            lows = int((parsed >= pd.Timestamp(today - timedelta(days=7))).sum())
        except Exception:
            lows = 0
    return {"week52_highs": highs, "week52_lows": lows}


def _fetch_fii_dii() -> Optional[list]:
    """FII/DII data — derive from market_watch_all_indices advances/declines
    since nselib doesn't expose a direct fii_dii endpoint in this version."""
    return None


def _fetch_vix() -> Optional[dict]:
    from nselib import capital_market
    vix = capital_market.india_vix_data(period="1M")
    if vix is None or vix.empty:
        return None
    last_row = vix.iloc[-1]
    return {
        "current": float(last_row.get("CLOSE_INDEX_VAL", last_row.get("CLOSE", 0))),
        "change": float(last_row.get("VIX_PTS_CHG", last_row.get("CHG", 0))),
    }


# ── Public API ────────────────────────────────────────────────────────

def get_market_breadth() -> dict:
    """Fetch market breadth indicators from NSE — all 4 calls in parallel."""
    ttl = _breadth_ttl()
    cached = _cache_get("market_breadth", ttl)
    if cached:
        return cached

    result: dict = {
        "advance_decline": None,
        "week52_highs": None,
        "week52_lows": None,
        "sector_heatmap": [],
        "fii_dii": None,
        "india_vix": None,
        "error": None,
    }

    tasks = {
        "heatmap": _fetch_heatmap,
        "w52": _fetch_52w_report,
        "fii_dii": _fetch_fii_dii,
        "vix": _fetch_vix,
    }

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                data = future.result(timeout=20)
                if name == "heatmap":
                    result["sector_heatmap"] = data or []
                elif name == "w52" and data:
                    result["week52_highs"] = data.get("week52_highs")
                    result["week52_lows"] = data.get("week52_lows")
                elif name == "fii_dii":
                    result["fii_dii"] = data
                elif name == "vix":
                    result["india_vix"] = data
            except Exception as e:
                logger.warning("Breadth sub-task '%s' failed: %s", name, e)

    # Derive advance/decline from the heatmap data
    if result["sector_heatmap"]:
        gainers = sum(1 for h in result["sector_heatmap"] if h["change"] > 0)
        losers = sum(1 for h in result["sector_heatmap"] if h["change"] < 0)
        result["advance_decline"] = {"advances": gainers, "declines": losers}

    _cache_set("market_breadth", result)
    return result


def get_sector_overview(holdings: list[dict], sector_map: dict[str, str]) -> dict:
    """Build sector-level analysis — index fetches run in parallel."""
    cached = _cache_get("sector_overview", _CACHE_TTL_SECTOR)
    if cached:
        return cached

    sector_holdings: dict[str, list] = {}
    sector_invested: dict[str, float] = {}

    for h in holdings:
        sym = h.get("symbol", "")
        sector = sector_map.get(sym, "Other")
        sector_holdings.setdefault(sector, []).append(h)
        sector_invested[sector] = sector_invested.get(sector, 0) + h.get("current", 0)

    total_portfolio = sum(sector_invested.values()) or 1

    active_sectors = [
        s for s in sorted(sector_invested, key=lambda s: sector_invested[s], reverse=True)
        if s not in ("ETF", "Other") and not s.startswith("ETF -")
    ]

    # Fetch all index performances in parallel
    index_perf_map: dict[str, Optional[dict]] = {}
    index_jobs = {}
    for sector in active_sectors:
        idx = SECTOR_INDEX_MAP.get(sector)
        if idx:
            index_jobs[sector] = idx

    with ThreadPoolExecutor(max_workers=min(len(index_jobs), 8)) as pool:
        futures = {
            pool.submit(_get_index_performance, idx): sector
            for sector, idx in index_jobs.items()
        }
        for future in as_completed(futures):
            sector = futures[future]
            try:
                index_perf_map[sector] = future.result(timeout=15)
            except Exception as e:
                logger.warning("Index fetch for sector '%s' failed: %s", sector, e)
                index_perf_map[sector] = None

    sectors = []
    for sector in active_sectors:
        invested = sector_invested[sector]
        weight = round(invested / total_portfolio * 100, 1)
        stock_list = sector_holdings.get(sector, [])
        stock_list.sort(key=lambda h: h.get("current", 0), reverse=True)

        index_name = SECTOR_INDEX_MAP.get(sector)

        sectors.append({
            "name": sector,
            "index_name": index_name or f"No index for {sector}",
            "weight": weight,
            "invested": round(invested, 2),
            "holdings": stock_list[:5],
            "holdings_count": len(stock_list),
            "index_performance": index_perf_map.get(sector),
            "driver": SECTOR_CYCLES.get(sector, ""),
        })

    result = {"sectors": sectors}
    _cache_set("sector_overview", result)
    return result


def _get_index_performance(index_name: str) -> Optional[dict]:
    """Fetch recent performance for a sector index."""
    cache_key = f"index_perf:{index_name}"
    cached = _cache_get(cache_key, _CACHE_TTL_SECTOR)
    if cached:
        return cached

    try:
        from nselib import capital_market

        today = datetime.now()
        from_date = (today - timedelta(days=365)).strftime("%d-%m-%Y")
        to_date = today.strftime("%d-%m-%Y")

        data = capital_market.index_data(index=index_name, from_date=from_date, to_date=to_date)
        if data is None or data.empty:
            return None

        close_col = None
        for col in ["CLOSE", "Close", "Closing Value"]:
            if col in data.columns:
                close_col = col
                break
        if not close_col:
            return None

        closes = data[close_col].astype(float)
        current = float(closes.iloc[-1])

        perf = {"current": current}

        for label, days in [("1w", 5), ("1m", 22), ("3m", 66), ("6m", 132), ("1y", 252)]:
            if len(closes) > days:
                past = float(closes.iloc[-days - 1])
                perf[label] = round((current - past) / past * 100, 2) if past else None
            else:
                perf[label] = None

        _cache_set(cache_key, perf)
        return perf

    except Exception as e:
        logger.warning("Index performance fetch for %s failed: %s", index_name, e)
        return None
