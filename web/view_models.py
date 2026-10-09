"""Row shapes for positions, orders and trades, and the broker-to-row mapping.

These exist so templates read ``row.pnl_pct`` instead of indexing raw broker
dictionaries, and so a field rename fails loudly here rather than silently
rendering blank in a template.

The ``*_rows`` functions are the one place a broker response becomes rows. The
server-rendered pages and the JSON endpoints the React app reads both call
them, so the two can never disagree about which broker field means what.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── Data classes for template rendering ────────────────────────────────────


@dataclass
class HoldingRow:
    symbol: str
    qty: int
    avg_price: float
    ltp: float



@dataclass
class PositionRow:
    symbol: str
    product: str
    net_qty: int
    buy_avg: float
    sell_avg: float
    ltp: float
    pnl: float



@dataclass
class OrderRow:
    orderid: str
    symbol: str
    txn_type: str
    qty: int
    price: float
    status: str
    time: str



@dataclass
class TradeRow:
    tradeid: str
    orderid: str
    symbol: str
    txn_type: str
    qty: int
    price: float
    time: str
    exchange: str
    product: str



# ── Broker response → rows ─────────────────────────────────────────────────


def _records(response: dict) -> list[dict]:
    """The ``data`` list of a broker response, or ``[]`` when there is none.

    Angel One answers "nothing today" with ``status: true, data: null``.
    """
    if not response or not response.get("status"):
        return []
    return response.get("data") or []


def _int(value) -> int:
    return int(value or 0)


def _float(value) -> float:
    return float(value or 0)


def position_rows(response: dict) -> list[PositionRow]:
    return [
        PositionRow(
            symbol=p.get("tradingsymbol", "N/A"),
            product=p.get("producttype", "N/A"),
            net_qty=_int(p.get("netqty")),
            buy_avg=_float(p.get("buyavgprice")),
            sell_avg=_float(p.get("sellavgprice")),
            ltp=_float(p.get("ltp")),
            pnl=_float(p.get("pnl")),
        )
        for p in _records(response)
    ]


def order_rows(response: dict) -> list[OrderRow]:
    return [
        OrderRow(
            orderid=o.get("orderid", "N/A"),
            symbol=o.get("tradingsymbol", "N/A"),
            txn_type=o.get("transactiontype", "N/A"),
            qty=_int(o.get("quantity")),
            price=_float(o.get("price")),
            status=o.get("status", "N/A"),
            time=o.get("updatetime", "N/A"),
        )
        for o in _records(response)
    ]


def trade_rows(response: dict) -> list[TradeRow]:
    # The trade book reports fills (``fillsize``/``fillprice``/``filltime``);
    # older payloads only carry the order's own fields, hence the fallbacks.
    return [
        TradeRow(
            tradeid=t.get("tradeid", "N/A"),
            orderid=t.get("orderid", "N/A"),
            symbol=t.get("tradingsymbol", "N/A"),
            txn_type=t.get("transactiontype", "N/A"),
            qty=_int(t.get("fillsize") or t.get("quantity")),
            price=_float(t.get("fillprice") or t.get("price")),
            time=t.get("filltime", t.get("updatetime", "N/A")),
            exchange=t.get("exchange", "N/A"),
            product=t.get("producttype", "N/A"),
        )
        for t in _records(response)
    ]
