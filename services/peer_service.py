"""Peer-relative valuation: where a metric sits within its own industry.

``services/fundamental_scoring.py`` grades each metric against absolute bands
from ``data/metric_glossary.json``. That is the right default — it is stable,
explainable, and the same bands the /learn page shows — but it cannot say
whether a P/E of 28 is expensive. In IT it is unremarkable; in cement it is
rich. The absolute verdict and the peer percentile answer different questions,
so this module adds the second one rather than replacing the first.

Peer groups come from NSE's own industry classification in the market-store
universe (451 names, exchange-maintained), not from ``data/sector_map.json``,
which is hand-maintained and covers 162 symbols.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

logger = logging.getLogger(__name__)

_cache: dict[str, dict] = {}
_CACHE_TTL = 6 * 3600  # matches the fundamental cache

# Metrics worth comparing against peers, and which direction is better.
# "low" means a smaller value ranks better (cheaper, less levered).
PEER_METRICS: dict[str, str] = {
    "pe_ratio": "low",
    "pb_ratio": "low",
    "ev_ebitda": "low",
    "peg_ratio": "low",
    "roe": "high",
    "roce": "high",
    "profit_margin": "high",
    "operating_margin": "high",
    "revenue_growth": "high",
    "earnings_growth": "high",
    "debt_to_equity": "low",
}

# Below this many peers with a value, a percentile is noise.
MIN_PEERS = 5

# How many peers to fetch. Each is a yfinance call, so this is the main cost.
MAX_PEERS_FETCHED = 24


def _cache_get(key: str) -> Optional[dict]:
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]
    return None


def get_peer_comparison(trading_symbol: str, *, max_peers: int = MAX_PEERS_FETCHED) -> dict:
    """Percentile rank of each metric against the stock's NSE industry peers.

    Returns ``percentiles`` keyed by metric, each with the raw value, the
    percentile (0-1, higher always meaning better), the peer median and the
    peer count. A metric with too few peers reporting is omitted rather than
    ranked against two companies.
    """
    cache_key = f"peers:{trading_symbol}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    from services.fundamental_service import get_stock_fundamentals
    from services.marketstore.universe import industry_of, load_universe, peers_of

    sym = _norm(trading_symbol)
    result: dict[str, Any] = {
        "symbol": trading_symbol,
        "industry": None,
        "peer_count": 0,
        "percentiles": {},
        "error": None,
    }

    try:
        universe = load_universe()
    except Exception as e:
        result["error"] = f"universe unavailable: {e}"
        return result

    industry = industry_of(sym, universe)
    if not industry:
        result["error"] = (f"{sym} is not in the NIFTY 500 universe, so it has no "
                           f"peer group here")
        return result

    result["industry"] = industry
    peers = peers_of(sym, universe)[:max_peers]
    if len(peers) < MIN_PEERS:
        result["error"] = f"only {len(peers)} peers in {industry}"
        return result

    own = get_stock_fundamentals(trading_symbol)
    if own.get("error") and not own.get("valuation"):
        result["error"] = own["error"]
        return result

    peer_data = _fetch_peers(peers)
    result["peer_count"] = len(peer_data)

    own_metrics = _metrics_of(own)
    for metric, better in PEER_METRICS.items():
        own_value = own_metrics.get(metric)
        if own_value is None:
            continue

        values = [m[metric] for m in peer_data if m.get(metric) is not None]
        if len(values) < MIN_PEERS:
            continue

        pct = _percentile(own_value, values, better)
        values_sorted = sorted(values)
        median = values_sorted[len(values_sorted) // 2]

        result["percentiles"][metric] = {
            "value": round(float(own_value), 3),
            "percentile": round(pct, 3),
            "peer_median": round(float(median), 3),
            "peers_reporting": len(values),
            "better": better,
            "reading": _reading(pct, metric),
        }

    result["summary"] = _summarise(result["percentiles"], industry)
    _cache[cache_key] = {"data": result, "ts": time.time()}
    return result


def _fetch_peers(peers: list[str]) -> list[dict]:
    """Fetch peer fundamentals in parallel.

    Mirrors the ThreadPoolExecutor pattern in ``fundamental_service`` — these
    are network-bound yfinance calls and run far faster concurrently. Each
    result rides that module's own 6-hour cache, so a second call for the same
    industry is nearly free.
    """
    from services.fundamental_service import get_stock_fundamentals

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(get_stock_fundamentals, p): p for p in peers}
        for future in futures:
            try:
                data = future.result(timeout=25)
            except Exception as e:
                logger.debug("peer fetch failed for %s: %s", futures[future], e)
                continue
            if data and not (data.get("error") and not data.get("valuation")):
                out.append(_metrics_of(data))
    return out


def _metrics_of(fundamentals: dict) -> dict:
    valuation = fundamentals.get("valuation") or {}
    health = fundamentals.get("health") or {}
    merged = {**valuation, **health}
    return {k: _num(merged.get(k)) for k in PEER_METRICS}


def _percentile(value: float, peers: list[float], better: str) -> float:
    """Fraction of peers this value beats, oriented so higher is always better."""
    if not peers:
        return 0.5
    if better == "low":
        wins = sum(1 for p in peers if value < p)
    else:
        wins = sum(1 for p in peers if value > p)
    return wins / len(peers)


def _reading(pct: float, metric: str) -> str:
    """Plain-English reading, oriented the same way as the percentile."""
    if pct >= 0.8:
        band = "much better than"
    elif pct >= 0.6:
        band = "better than"
    elif pct > 0.4:
        band = "in line with"
    elif pct > 0.2:
        band = "worse than"
    else:
        band = "much worse than"
    return f"{band} its industry ({pct:.0%} percentile)"


def _summarise(percentiles: dict, industry: str) -> str:
    if not percentiles:
        return f"Not enough peer data in {industry} to compare."

    valuation_keys = ("pe_ratio", "pb_ratio", "ev_ebitda", "peg_ratio")
    quality_keys = ("roe", "roce", "profit_margin", "operating_margin")

    val = [v["percentile"] for k, v in percentiles.items() if k in valuation_keys]
    qual = [v["percentile"] for k, v in percentiles.items() if k in quality_keys]

    parts = []
    if val:
        avg = sum(val) / len(val)
        parts.append("cheaper than most peers" if avg >= 0.6
                     else "more expensive than most peers" if avg <= 0.4
                     else "priced in line with peers")
    if qual:
        avg = sum(qual) / len(qual)
        parts.append("with stronger returns" if avg >= 0.6
                     else "with weaker returns" if avg <= 0.4
                     else "with comparable returns")

    return f"Against {industry}: " + ", ".join(parts) + "." if parts else \
        f"Against {industry}: mixed."


def enrich_with_peers(fundamentals: dict) -> dict:
    """Attach the peer block to a fundamentals payload, non-fatally.

    Used by the research API so the absolute score and the peer percentile
    arrive together; a peer failure must not take down the page.
    """
    out = dict(fundamentals)
    try:
        out["peers"] = get_peer_comparison(fundamentals.get("symbol", ""))
    except Exception as e:
        logger.warning("peer comparison failed for %s: %s",
                       fundamentals.get("symbol"), e)
        out["peers"] = {"error": str(e), "percentiles": {}}
    return out


def _num(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _norm(symbol: str) -> str:
    s = str(symbol).strip().upper()
    for suffix in ("-EQ", "-BE", ".NS"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s
