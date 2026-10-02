"""Row shapes passed from page handlers into Jinja templates.

These exist so templates read ``row.pnl_pct`` instead of indexing raw broker
dictionaries, and so a field rename fails loudly here rather than silently
rendering blank in a template.
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
