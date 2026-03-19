"""
Shared portfolio snapshot builder for web AI, LangGraph agent, and MCP parity.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_portfolio_data(client: Any) -> dict:
    """Extract holdings, positions, and funds into a dict for LLM / agent tools."""
    data: dict = {"holdings": [], "summary": {}, "funds": {}}

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
                data["holdings"].append({
                    "symbol": h.get("tradingsymbol", "N/A"),
                    "qty": qty,
                    "avg_price": avg,
                    "ltp": ltp,
                    "invested": round(inv, 2),
                    "current": round(cur, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
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
