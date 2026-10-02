"""Portfolio aggregation.

``build_portfolio_data`` makes three broker calls, so every caller should go
through ``get_portfolio_cached`` instead. Hitting the uncached function from
several endpoints during one page load is what trips Angel One's rate limit.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_portfolio_cache: dict[str, tuple[float, dict]] = {}


_PORTFOLIO_CACHE_TTL = 60  # seconds


def get_portfolio_cached(sid: str, client) -> dict:
    """Return portfolio data, using a short-lived cache to avoid Angel rate limits."""
    entry = _portfolio_cache.get(sid)
    if entry and (time.time() - entry[0]) < _PORTFOLIO_CACHE_TTL:
        return entry[1]
    data = build_portfolio_data(client)
    _portfolio_cache[sid] = (time.time(), data)
    return data



# ── AI API endpoints ──────────────────────────────────────────────────────


def build_portfolio_data(client) -> dict:
    """Extract holdings, positions, and funds into a dict for the LLM."""
    data = {"holdings": [], "summary": {}, "funds": {}}

    try:
        h_data = client.get_holdings()
        if h_data.get("status") and h_data.get("data"):
            total_inv = 0.0
            total_cur = 0.0
            for h in h_data["data"]:
                qty = int(h.get("quantity", 0) or 0)
                avg = float(h.get("averageprice", 0) or 0)
                ltp = float(h.get("ltp", 0) or 0)
                inv = qty * avg
                cur = qty * ltp
                pnl = cur - inv
                pnl_pct = (pnl / inv * 100) if inv else 0.0
                total_inv += inv
                total_cur += cur
                tok = h.get("symboltoken") or h.get("symbolToken") or ""
                data["holdings"].append({
                    "symbol": h.get("tradingsymbol", "N/A"),
                    "symboltoken": str(tok).strip() if tok else "",
                    "qty": qty, "avg_price": avg, "ltp": ltp,
                    "invested": round(inv, 2), "current": round(cur, 2),
                    "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                })
            data["summary"]["total_invested"] = round(total_inv, 2)
            data["summary"]["current_value"] = round(total_cur, 2)
            data["summary"]["overall_pnl"] = round(total_cur - total_inv, 2)
            data["summary"]["overall_pnl_pct"] = round(
                ((total_cur - total_inv) / total_inv * 100) if total_inv else 0.0, 2
            )
    except Exception as e:
        logger.warning("Portfolio build – holdings error: %s", e)

    try:
        pos = client.get_positions()
        if pos.get("status") and pos.get("data"):
            data["summary"]["day_pnl"] = round(
                sum(float(p.get("pnl", 0) or 0) for p in pos["data"]), 2
            )
    except Exception as e:
        logger.warning("Portfolio build – positions error: %s", e)

    try:
        funds = client.get_funds()
        if funds.get("status") and funds.get("data"):
            d = funds["data"]
            data["funds"]["available_cash"] = d.get("availablecash", "N/A")
            data["funds"]["net"] = d.get("net", "N/A")
    except Exception as e:
        logger.warning("Portfolio build – funds error: %s", e)

    return data



def compute_beta(x_returns, y_returns):
    n = len(x_returns)
    mean_x = sum(x_returns) / n
    mean_y = sum(y_returns) / n

    cov = sum((x_returns[i] - mean_x) * (y_returns[i] - mean_y) for i in range(n)) / n
    var_x = sum((x_returns[i] - mean_x) ** 2 for i in range(n)) / n

    beta = cov / var_x if var_x > 0 else 1.0
    alpha = mean_y - beta * mean_x

    ss_res = sum((y_returns[i] - (alpha + beta * x_returns[i])) ** 2 for i in range(n))
    ss_tot = sum((y_returns[i] - mean_y) ** 2 for i in range(n))
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return {"beta": round(beta, 4), "alpha": round(alpha, 6), "r_squared": round(r_squared, 4)}
