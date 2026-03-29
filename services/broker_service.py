"""
Structured Angel One / SmartAPI helpers for reuse by MCP tools and ADK agents.

Returns JSON-friendly dicts; presentation formatting stays in mcp_server where needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def portfolio_summary_as_dict(client: Any) -> dict[str, Any]:
    """High-level portfolio snapshot: holdings totals, day P&L, funds."""
    out: dict[str, Any] = {
        "holdings_summary": None,
        "day_positions": None,
        "funds": None,
        "errors": [],
    }
    try:
        all_h = client.get_all_holdings()
        if all_h.get("status") and all_h.get("data"):
            d = all_h["data"]
            out["holdings_summary"] = {
                "total_investment": float(d.get("totalholdingvalue", 0) or 0),
                "current_value": float(d.get("totalcurrentvalue", 0) or 0),
                "overall_pnl_percent": float(d.get("totalpnlpercentage", 0) or 0),
            }
        else:
            out["errors"].append(
                {"section": "holdings", "message": all_h.get("message", "No holdings data")}
            )
    except Exception as e:
        logger.exception("portfolio_summary holdings")
        out["errors"].append({"section": "holdings", "message": str(e)})

    try:
        positions = client.get_positions()
        if positions.get("status") and positions.get("data"):
            day_pnl = sum(float(p.get("pnl", 0) or 0) for p in positions["data"])
            out["day_positions"] = {"day_pnl": day_pnl, "open_count": len(positions["data"])}
        else:
            out["day_positions"] = {"day_pnl": 0.0, "open_count": 0, "note": "no_open_positions"}
    except Exception as e:
        logger.exception("portfolio_summary positions")
        out["errors"].append({"section": "positions", "message": str(e)})

    try:
        funds = client.get_funds()
        if funds.get("status") and funds.get("data"):
            d = funds["data"]
            out["funds"] = {
                "available_cash": d.get("availablecash"),
                "net": d.get("net"),
            }
        else:
            out["errors"].append(
                {"section": "funds", "message": funds.get("message", "No funds data")}
            )
    except Exception as e:
        logger.exception("portfolio_summary funds")
        out["errors"].append({"section": "funds", "message": str(e)})

    return out


def fetch_stock_history_candles(
    client: Any,
    symbol: str,
    days: int = 30,
    interval: str = "ONE_DAY",
) -> dict[str, Any]:
    """Resolve symbol and fetch OHLCV candles. Returns ok, metadata, candles or error."""
    days = max(1, min(int(days), 365))
    scrip = client.search_scrip("NSE", symbol)
    if not scrip.get("status") or not scrip.get("data"):
        return {
            "ok": False,
            "error": f"Could not find symbol '{symbol}' on NSE.",
        }

    match = scrip["data"][0]
    token = match["symboltoken"]
    tradingsymbol = match["tradingsymbol"]
    exchange = match["exchange"]

    todate = datetime.now()
    fromdate = todate - timedelta(days=days)
    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": interval,
        "fromdate": fromdate.strftime("%Y-%m-%d %H:%M"),
        "todate": todate.strftime("%Y-%m-%d %H:%M"),
    }
    result = client.get_candle_data(params)
    if not result.get("status") or not result.get("data"):
        return {
            "ok": False,
            "error": result.get("message", "unknown"),
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
        }

    candles = result["data"]
    summary: dict[str, Any] | None = None
    if len(candles) >= 2:
        first_close = candles[0][4]
        last_close = candles[-1][4]
        change = last_close - first_close
        change_pct = (change / first_close * 100) if first_close else 0
        summary = {
            "period_change": float(change),
            "period_change_percent": float(change_pct),
        }

    return {
        "ok": True,
        "tradingsymbol": tradingsymbol,
        "exchange": exchange,
        "days": days,
        "interval": interval,
        "candle_count": len(candles),
        "candles": candles,
        "summary": summary,
    }


def calculate_symbol_pnl(client: Any, symbol: str) -> dict[str, Any]:
    """P&L breakdown for a symbol from holdings + intraday positions."""
    symbol_upper = symbol.upper().strip()
    holding_match = None
    position_matches: list = []

    try:
        holdings = client.get_holdings()
        if holdings.get("status") and holdings.get("data"):
            for h in holdings["data"]:
                ts = (h.get("tradingsymbol") or "").upper()
                if symbol_upper in ts:
                    holding_match = h
                    break
    except Exception as e:
        return {"ok": False, "error": f"holdings: {e}"}

    try:
        positions = client.get_positions()
        if positions.get("status") and positions.get("data"):
            for p in positions["data"]:
                ts = (p.get("tradingsymbol") or "").upper()
                if symbol_upper in ts:
                    position_matches.append(p)
    except Exception as e:
        return {"ok": False, "error": f"positions: {e}"}

    if not holding_match and not position_matches:
        return {
            "ok": False,
            "error": f"No holdings or positions matching '{symbol}'.",
        }

    holding_block = None
    if holding_match:
        h = holding_match
        qty = int(h.get("quantity", 0) or 0)
        avg = float(h.get("averageprice", 0) or 0)
        ltp = float(h.get("ltp", 0) or 0)
        invested = qty * avg
        current = qty * ltp
        pnl = current - invested
        pnl_pct = (pnl / invested * 100) if invested else 0.0
        holding_block = {
            "tradingsymbol": h.get("tradingsymbol"),
            "quantity": qty,
            "average_price": avg,
            "ltp": ltp,
            "invested": invested,
            "current_value": current,
            "unrealized_pnl": pnl,
            "unrealized_pnl_percent": pnl_pct,
        }

    position_blocks = []
    total_day_pnl = 0.0
    for p in position_matches:
        pnl = float(p.get("pnl", 0) or 0)
        total_day_pnl += pnl
        position_blocks.append(
            {
                "tradingsymbol": p.get("tradingsymbol"),
                "producttype": p.get("producttype"),
                "netqty": int(p.get("netqty", 0) or 0),
                "buyavgprice": float(p.get("buyavgprice", 0) or 0),
                "sellavgprice": float(p.get("sellavgprice", 0) or 0),
                "ltp": float(p.get("ltp", 0) or 0),
                "day_pnl": pnl,
            }
        )

    total_pnl = 0.0
    if holding_match:
        qty = int(holding_match.get("quantity", 0) or 0)
        avg = float(holding_match.get("averageprice", 0) or 0)
        ltp = float(holding_match.get("ltp", 0) or 0)
        total_pnl += (qty * ltp) - (qty * avg)
    for p in position_matches:
        total_pnl += float(p.get("pnl", 0) or 0)

    return {
        "ok": True,
        "symbol": symbol_upper,
        "holding": holding_block,
        "positions": position_blocks,
        "positions_day_pnl_total": total_day_pnl,
        "combined_pnl_estimate": total_pnl,
    }


def fetch_market_depth(client: Any, symbol: str, exchange: str = "NSE") -> dict[str, Any]:
    scrip = client.search_scrip(exchange, symbol)
    if not scrip.get("status") or not scrip.get("data"):
        return {"ok": False, "error": f"Could not find symbol '{symbol}' on {exchange}."}

    match = scrip["data"][0]
    token = match["symboltoken"]
    tradingsymbol = match["tradingsymbol"]

    result = client.get_market_data("FULL", {exchange: [token]})
    if not result.get("status") or not result.get("data"):
        return {
            "ok": False,
            "error": result.get("message", "unknown"),
            "tradingsymbol": tradingsymbol,
        }

    data = result["data"]
    fetched = data.get("fetched", [{}])
    if not fetched:
        return {"ok": False, "error": "No market data returned", "tradingsymbol": tradingsymbol}

    stock = fetched[0]
    return {
        "ok": True,
        "tradingsymbol": tradingsymbol,
        "exchange": exchange,
        "ltp": stock.get("ltp"),
        "open": stock.get("open"),
        "high": stock.get("high"),
        "low": stock.get("low"),
        "close": stock.get("close"),
        "tot_buy_qty": stock.get("totBuyQuan"),
        "tot_sell_qty": stock.get("totSellQuan"),
        "depth": stock.get("depth", {}),
    }


def place_order_result(client: Any, order_params: dict) -> dict[str, Any]:
    try:
        result = client.place_order(order_params)
        if result:
            return {"ok": True, "order_id": str(result), "raw": result}
        return {"ok": False, "error": "Order placement failed (no id returned)."}
    except Exception as e:
        logger.exception("place_order")
        return {"ok": False, "error": str(e)}


def modify_order_result(client: Any, order_params: dict) -> dict[str, Any]:
    try:
        result = client.modify_order(order_params)
        if isinstance(result, dict) and not result.get("status"):
            return {
                "ok": False,
                "error": result.get("message", str(result)),
                "raw": result,
            }
        return {"ok": True, "raw": result}
    except Exception as e:
        logger.exception("modify_order")
        return {"ok": False, "error": str(e)}


def holdings_for_sector_analysis(client: Any) -> list[dict]:
    """Normalize Angel holdings into rows for services.sector_service.get_sector_overview."""
    rows: list[dict] = []
    try:
        h_data = client.get_holdings()
        if not h_data.get("status") or not h_data.get("data"):
            return rows
        for h in h_data["data"]:
            qty = int(h.get("quantity", 0) or 0)
            avg = float(h.get("averageprice", 0) or 0)
            ltp = float(h.get("ltp", 0) or 0)
            inv = qty * avg
            cur = qty * ltp
            pnl = cur - inv
            pnl_pct = (pnl / inv * 100) if inv else 0.0
            rows.append(
                {
                    "symbol": h.get("tradingsymbol", "N/A"),
                    "qty": qty,
                    "avg_price": avg,
                    "ltp": ltp,
                    "invested": round(inv, 2),
                    "current": round(cur, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                }
            )
    except Exception as e:
        logger.exception("holdings_for_sector_analysis: %s", e)
    return rows


def cancel_order_result(client: Any, order_id: str, variety: str = "NORMAL") -> dict[str, Any]:
    try:
        result = client.cancel_order(order_id, variety)
        if isinstance(result, dict) and not result.get("status"):
            return {
                "ok": False,
                "error": result.get("message", str(result)),
                "raw": result,
            }
        return {"ok": True, "raw": result}
    except Exception as e:
        logger.exception("cancel_order")
        return {"ok": False, "error": str(e)}
