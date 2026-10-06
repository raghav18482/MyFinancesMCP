"""Ownership and flows, behind a source interface.

What is genuinely obtainable for NSE stocks from the pinned dependencies:
  * bulk and block deals                 — nselib
  * delivery percentage and its trend    — the local market store
  * short selling                        — nselib
  * promoter/insider holding             — yfinance ``major_holders``
  * insider transactions                 — yfinance

What is **not** obtainable, and is therefore reported as unknown rather than
omitted: the quarterly shareholding pattern — the promoter / FII / DII /
public split. yfinance's ``institutional_holders`` and ``mutualfund_holders``
return empty frames for NSE tickers (verified), and nselib has no endpoint for
it. Getting it means either a paid feed or scraping NSE's undocumented JSON
API, which breaks without warning.

So the shape of the answer is a ``ShareholdingSource`` protocol with a null
implementation that says "no source configured". A paid feed drops in by
registering a different implementation; no caller changes. The panel can then
say it does not know, which is a more useful thing for an analyst to read than
a quietly missing section.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Optional, Protocol, runtime_checkable

import pandas as pd

logger = logging.getLogger(__name__)

_cache: dict[str, dict] = {}
_CACHE_TTL = 12 * 3600  # ownership moves slowly


# ── The pluggable gap ──────────────────────────────────────────────────────
@runtime_checkable
class ShareholdingSource(Protocol):
    """Quarterly promoter / FII / DII / public split.

    Implement and register with ``set_shareholding_source`` when a feed exists.
    """

    name: str

    def fetch(self, symbol: str) -> Optional[dict]:
        """Return the latest pattern, or None when unavailable."""


class NullShareholdingSource:
    """The honest default: says it has no data instead of returning zeros."""

    name = "none"

    def fetch(self, symbol: str) -> Optional[dict]:
        return None


_shareholding_source: ShareholdingSource = NullShareholdingSource()


def set_shareholding_source(source: ShareholdingSource) -> None:
    global _shareholding_source
    _shareholding_source = source
    logger.info("ownership: shareholding source set to %r", getattr(source, "name", source))


# ── Public API ─────────────────────────────────────────────────────────────
def get_ownership(trading_symbol: str, *, lookback_days: int = 90) -> dict:
    """Ownership and flow signals for one stock.

    Every section states its source, and the shareholding section states
    plainly when no source is configured.
    """
    cache_key = f"ownership:{trading_symbol}:{lookback_days}"
    entry = _cache.get(cache_key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]

    sym = _norm(trading_symbol)
    result: dict[str, Any] = {
        "symbol": trading_symbol,
        "delivery": _delivery_profile(sym, lookback_days),
        "bulk_deals": _deals(sym, "bulk", lookback_days),
        "block_deals": _deals(sym, "block", lookback_days),
        "insider": _insider(trading_symbol),
        "shareholding_pattern": None,
        "sources": {
            "delivery": "NSE bhavcopy via the local market store",
            "deals": "nselib bulk_deal_data / block_deals_data",
            "insider": "yfinance major_holders / insider_transactions",
        },
    }

    pattern = _shareholding_source.fetch(sym)
    if pattern is None:
        result["shareholding_pattern"] = None
        result["shareholding_unavailable_reason"] = (
            "No quarterly shareholding source is configured. The promoter / FII / "
            "DII split is not available from yfinance or nselib for NSE tickers; "
            "it needs a paid feed or an NSE filings adapter. Treat this as unknown, "
            "not as zero."
        )
    else:
        result["shareholding_pattern"] = pattern
        result["sources"]["shareholding"] = getattr(_shareholding_source, "name", "custom")

    result["summary"] = _summarise(result)
    _cache[cache_key] = {"data": result, "ts": time.time()}
    return result


def _delivery_profile(symbol: str, lookback_days: int) -> dict:
    """Delivery percentage level and trend — a conviction proxy.

    High delivery means buyers took the shares rather than squaring off, so a
    move on rising delivery is better supported than the same move on falling
    delivery.
    """
    try:
        from services.marketstore import read_range

        end = date.today()
        start = end - timedelta(days=int(lookback_days * 1.6))
        rows = read_range([symbol], start, end)
    except Exception as e:
        return {"error": f"market store unavailable: {e}"}

    if rows.empty or "deliv_pct" not in rows.columns:
        return {"error": "no stored delivery data for this symbol"}

    rows = rows.sort_values("date")
    deliv = pd.to_numeric(rows["deliv_pct"], errors="coerce").dropna()
    if deliv.empty:
        return {"error": "no delivery values"}

    recent, prior = deliv.tail(20), deliv.tail(60).head(40)
    out = {
        "latest_pct": round(float(deliv.iloc[-1]), 2),
        "avg_20d_pct": round(float(recent.mean()), 2),
        "sessions": int(len(deliv)),
        "as_of": pd.Timestamp(rows["date"].iloc[-1]).date().isoformat(),
    }
    if len(prior) >= 10:
        shift = float(recent.mean() - prior.mean())
        out["shift_vs_prior_pct"] = round(shift, 2)
        out["trend"] = "rising" if shift > 2 else "falling" if shift < -2 else "stable"

    turnover = pd.to_numeric(rows["turnover"], errors="coerce").dropna().tail(20)
    if not turnover.empty:
        out["adv_20d_value"] = round(float(turnover.mean()), 2)

    return out


def _deals(symbol: str, kind: str, lookback_days: int) -> dict:
    """Bulk or block deals for this symbol in the recent window."""
    period = "1M" if lookback_days <= 35 else "3M" if lookback_days <= 100 else "6M"
    try:
        from nselib import capital_market

        fn = capital_market.bulk_deal_data if kind == "bulk" else capital_market.block_deals_data
        df = fn(period=period)
    except Exception as e:
        logger.debug("ownership: %s deals unavailable (%s)", kind, e)
        return {"error": str(e), "deals": []}

    if df is None or df.empty:
        return {"deals": [], "count": 0}

    col = next((c for c in df.columns if c.strip().lower() == "symbol"), None)
    if col is None:
        return {"deals": [], "count": 0}

    hits = df[df[col].astype(str).str.strip().str.upper() == symbol]
    if hits.empty:
        return {"deals": [], "count": 0, "window": period}

    deals = []
    for _, row in hits.head(25).iterrows():
        deals.append({
            "date": str(row.get("Date", "")).strip(),
            "client": str(row.get("ClientName", "")).strip(),
            "side": str(row.get("Buy/Sell", "")).strip().upper(),
            "quantity": _int(row.get("QuantityTraded")),
            "price": _num(row.get("TradePrice/Wght.Avg.Price")),
        })

    buys = sum(1 for d in deals if d["side"].startswith("B"))
    return {
        "deals": deals,
        "count": len(deals),
        "window": period,
        "buy_count": buys,
        "sell_count": len(deals) - buys,
    }


def _insider(trading_symbol: str) -> dict:
    """Promoter/insider holding and recent insider transactions, from yfinance."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(_yf_symbol(trading_symbol))
        holders = ticker.major_holders
        txns = ticker.insider_transactions
    except Exception as e:
        return {"error": str(e)}

    out: dict[str, Any] = {}

    if holders is not None and not holders.empty:
        # Match the exact yfinance keys rather than substrings. A substring
        # match on "institution" also catches institutionsCount, which is a
        # count of institutions — 383 of them reads as 383% of the company.
        fields = {
            "insiderspercentheld": ("insider_holding_pct", "fraction"),
            "institutionspercentheld": ("institution_holding_pct", "fraction"),
            "institutionsfloatpercentheld": ("institution_float_pct", "fraction"),
            "institutionscount": ("institution_count", "count"),
        }
        for idx, row in holders.iterrows():
            spec = fields.get(str(idx).strip().lower())
            value = _num(row.iloc[0]) if len(row) else None
            if spec is None or value is None:
                continue
            name, kind = spec
            out[name] = int(value) if kind == "count" else round(value * 100, 2)

    if txns is not None and not txns.empty:
        recent = []
        for _, row in txns.head(10).iterrows():
            recent.append({
                "insider": str(row.get("Insider", "")).strip(),
                "transaction": str(row.get("Transaction", "")).strip(),
                "shares": _int(row.get("Shares")),
                "date": str(row.get("Start Date", "")).strip()[:10],
            })
        out["recent_transactions"] = recent

    return out or {"error": "no insider data"}


def _summarise(result: dict) -> str:
    bits = []

    deliv = result.get("delivery") or {}
    if deliv.get("avg_20d_pct") is not None:
        trend = deliv.get("trend")
        bit = f"delivery averaging {deliv['avg_20d_pct']:.0f}% over 20 sessions"
        if trend and trend != "stable":
            bit += f" and {trend}"
        bits.append(bit)

    for kind in ("bulk_deals", "block_deals"):
        block = result.get(kind) or {}
        if block.get("count"):
            label = kind.replace("_", " ")
            bits.append(f"{block['count']} {label} ({block.get('buy_count', 0)} buy, "
                        f"{block.get('sell_count', 0)} sell)")

    insider = result.get("insider") or {}
    if insider.get("insider_holding_pct") is not None:
        bits.append(f"insider holding {insider['insider_holding_pct']:.1f}%")

    if not bits:
        return "No ownership signals available."

    summary = "; ".join(bits).capitalize() + "."
    if result.get("shareholding_pattern") is None:
        summary += (" Quarterly promoter/FII/DII split is unknown — no source "
                    "configured.")
    return summary


def _yf_symbol(trading_symbol: str) -> str:
    base = _norm(trading_symbol)
    return f"{base}.NS"


def _norm(symbol: str) -> str:
    s = str(symbol).strip().upper()
    for suffix in ("-EQ", "-BE", ".NS"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def _num(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _int(val) -> Optional[int]:
    f = _num(val)
    return int(f) if f is not None else None
