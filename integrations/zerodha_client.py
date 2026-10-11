"""Kite Connect client with the Angel-compatible methods used by this app.

The compatibility boundary keeps the existing portfolio, analytics and order
workflows broker-agnostic while preserving Kite's native calls internally.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")


def _ok(data: Any) -> dict[str, Any]:
    return {"status": True, "data": data, "message": "Success"}


def _failed(exc: Exception) -> dict[str, Any]:
    return {"status": False, "data": None, "message": str(exc)}


class ZerodhaClient:
    """Adapter for the account methods called by existing application flows."""

    def __init__(self, api_key: str, access_token: str, profile: dict[str, Any] | None = None,
                 expires_at: datetime | None = None):
        self.api_key = api_key
        self.access_token = access_token
        self.smart_api = KiteConnect(api_key=api_key)
        self.smart_api.set_access_token(access_token)
        self._profile = profile or {}
        self.client_id = str(self._profile.get("user_id") or "")
        self.client_code = self.client_id
        self.broker = "zerodha"
        self._instruments: dict[tuple[str, str], dict[str, Any]] = {}
        self.expires_at = expires_at or self._next_expiry()

    @staticmethod
    def _next_expiry() -> datetime:
        now = datetime.now(_IST)
        expiry = datetime.combine(now.date(), time(6), tzinfo=_IST)
        if now >= expiry:
            expiry += timedelta(days=1)
        return expiry.astimezone(timezone.utc)

    def ensure_session(self) -> None:
        if datetime.now(timezone.utc) >= self.expires_at:
            raise RuntimeError("Zerodha session expired. Connect Zerodha again to continue.")

    @property
    def feed_token(self):
        return None

    @property
    def auth_token(self):
        return self.access_token

    @property
    def password(self):
        return None

    @property
    def totp_secret(self):
        return None

    def get_profile(self):
        try:
            profile = self.smart_api.profile()
            self._profile = profile
            self.client_id = str(profile.get("user_id") or self.client_id)
            return _ok(profile)
        except Exception as exc:
            return _failed(exc)

    def get_holdings(self):
        try:
            data = self.smart_api.holdings()
            return _ok([{
                **h,
                "tradingsymbol": h.get("tradingsymbol"),
                "symboltoken": str(h.get("instrument_token") or ""),
                "quantity": h.get("quantity", 0),
                "averageprice": h.get("average_price", 0),
                "ltp": h.get("last_price", 0),
                "exchange": h.get("exchange", "NSE"),
            } for h in data])
        except Exception as exc:
            return _failed(exc)

    def get_all_holdings(self):
        result = self.get_holdings()
        if not result["status"]:
            return result
        holdings = result["data"]
        invested = sum(float(h.get("averageprice") or 0) * int(h.get("quantity") or 0) for h in holdings)
        current = sum(float(h.get("ltp") or 0) * int(h.get("quantity") or 0) for h in holdings)
        pnl_pct = (current - invested) / invested * 100 if invested else 0
        return _ok({"totalholdingvalue": invested, "totalcurrentvalue": current,
                    "totalpnlpercentage": pnl_pct, "holdings": holdings})

    def get_positions(self):
        try:
            positions = self.smart_api.positions()
            output = []
            for p in positions.get("net", []):
                output.append({
                    **p,
                    "tradingsymbol": p.get("tradingsymbol"),
                    "symboltoken": str(p.get("instrument_token") or ""),
                    "exchange": p.get("exchange", "NSE"),
                    "netqty": p.get("quantity", 0),
                    "quantity": p.get("quantity", 0),
                    "producttype": p.get("product", ""),
                    "buyavgprice": p.get("buy_price", 0),
                    "sellavgprice": p.get("sell_price", 0),
                    "ltp": p.get("last_price", 0),
                    "pnl": p.get("pnl", 0),
                })
            return _ok(output)
        except Exception as exc:
            return _failed(exc)

    def get_order_book(self):
        try:
            return _ok([self._order_row(o) for o in self.smart_api.orders()])
        except Exception as exc:
            return _failed(exc)

    def get_trade_book(self):
        try:
            return _ok([self._trade_row(t) for t in self.smart_api.trades()])
        except Exception as exc:
            return _failed(exc)

    @staticmethod
    def _order_row(o: dict[str, Any]) -> dict[str, Any]:
        return {**o, "orderid": o.get("order_id"), "quantity": o.get("quantity", 0),
                "transactiontype": o.get("transaction_type"), "variety": o.get("variety", "regular"),
                "updatetime": o.get("order_timestamp"), "tradingsymbol": o.get("tradingsymbol")}

    @staticmethod
    def _trade_row(t: dict[str, Any]) -> dict[str, Any]:
        return {**t, "orderid": t.get("order_id"), "tradingsymbol": t.get("tradingsymbol"),
                "transactiontype": t.get("transaction_type"), "quantity": t.get("quantity", 0),
                "averageprice": t.get("average_price", 0), "tradeid": t.get("trade_id"),
                "fillsize": t.get("quantity", 0), "fillprice": t.get("average_price", 0),
                "filltime": t.get("fill_timestamp"), "producttype": t.get("product", "")}

    def get_funds(self):
        try:
            margins = self.smart_api.margins("equity")
            available = margins.get("available", {})
            return _ok({"availablecash": available.get("cash", 0),
                        "net": margins.get("net", 0), "available": available})
        except Exception as exc:
            return _failed(exc)

    def _instrument_map(self, exchange: str) -> dict[str, Any]:
        self.ensure_session()
        if exchange not in {"NSE", "BSE", "NFO", "BFO", "MCX", "CDS"}:
            raise ValueError("Unsupported exchange")
        # Instrument dump is refreshed per client to keep lookups stable without
        # repeatedly downloading a large catalogue on every search.
        rows = self._instruments.get((exchange, "catalog"))
        if rows is None:
            rows = {f'{r.get("tradingsymbol", "").upper()}:{r.get("exchange")}': r
                    for r in self.smart_api.instruments(exchange)}
            self._instruments[(exchange, "catalog")] = rows
        return rows

    def search_scrip(self, exchange: str, search_text: str):
        try:
            rows = self._instrument_map(exchange)
            term = search_text.upper().strip()
            matches = [r for r in rows.values() if term in r.get("tradingsymbol", "").upper()]
            matches.sort(key=lambda r: (r.get("tradingsymbol", "").upper() != term,
                                       r.get("instrument_type") != "EQ"))
            return _ok([{"tradingsymbol": r["tradingsymbol"], "symboltoken": str(r["instrument_token"]),
                         "exchange": r["exchange"], "name": r.get("name", "")} for r in matches[:50]])
        except Exception as exc:
            return _failed(exc)

    def get_ltp(self, exchange: str, tradingsymbol: str, symboltoken: str):
        try:
            q = self.smart_api.ltp(f"{exchange}:{tradingsymbol}")
            value = next(iter(q.values()))
            return _ok({**value, "ltp": value.get("last_price")})
        except Exception as exc:
            return _failed(exc)

    def get_candle_data(self, params: dict[str, Any]):
        try:
            exchange = params["exchange"]
            token = int(params["symboltoken"])
            start = datetime.strptime(params["fromdate"], "%Y-%m-%d %H:%M")
            end = datetime.strptime(params["todate"], "%Y-%m-%d %H:%M")
            intervals = {"ONE_MINUTE": "minute", "FIVE_MINUTE": "5minute", "FIFTEEN_MINUTE": "15minute",
                         "THIRTY_MINUTE": "30minute", "ONE_HOUR": "60minute", "ONE_DAY": "day"}
            interval = intervals.get(params.get("interval", "ONE_DAY"))
            if interval is None:
                raise ValueError("Unsupported candle interval")
            candles = self.smart_api.historical_data(token, start, end, interval)
            return _ok([[c["date"].isoformat(), c["open"], c["high"], c["low"], c["close"], c["volume"]]
                        for c in candles])
        except Exception as exc:
            return _failed(exc)

    def get_market_data(self, mode: str, exchange_tokens: dict[str, list[str]]):
        try:
            keys = []
            for exchange, tokens in exchange_tokens.items():
                rows = self._instrument_map(exchange)
                by_token = {str(r["instrument_token"]): r for r in rows.values()}
                keys.extend(f"{exchange}:{by_token[str(t)]['tradingsymbol']}" for t in tokens if str(t) in by_token)
            quotes = self.smart_api.quote(*keys) if mode.upper() == "FULL" else self.smart_api.ltp(*keys)
            fetched = []
            for q in quotes.values():
                fetched.append({"ltp": q.get("last_price"), "open": q.get("ohlc", {}).get("open"),
                                "high": q.get("ohlc", {}).get("high"), "low": q.get("ohlc", {}).get("low"),
                                "close": q.get("ohlc", {}).get("close"), "totBuyQuan": 0, "totSellQuan": 0,
                                "depth": q.get("depth", {})})
            return _ok({"fetched": fetched})
        except Exception as exc:
            return _failed(exc)

    def place_order(self, params: dict[str, Any]):
        try:
            order_id = self.smart_api.place_order(
                variety=self._variety(params.get("variety", "NORMAL")),
                exchange=params.get("exchange", "NSE"),
                tradingsymbol=params["tradingsymbol"],
                transaction_type=params.get("transactiontype", "BUY").upper(),
                quantity=int(params["quantity"]),
                product=self._product(params.get("producttype", "DELIVERY")),
                order_type=self._order_type(params.get("ordertype", "MARKET")),
                price=float(params.get("price", 0) or 0) or None,
                trigger_price=float(params.get("triggerprice", 0) or 0) or None,
                validity=params.get("duration", "DAY").upper(),
            )
            return _ok(order_id)
        except Exception as exc:
            return _failed(exc)

    def modify_order(self, params: dict[str, Any]):
        try:
            order_id = self.smart_api.modify_order(
                variety=self._variety(params.get("variety", "NORMAL")),
                order_id=params.get("orderid") or params.get("order_id"),
                quantity=int(params["quantity"]) if params.get("quantity") else None,
                price=float(params["price"]) if params.get("price") else None,
                order_type=self._order_type(params["ordertype"]) if params.get("ordertype") else None,
                trigger_price=float(params["triggerprice"]) if params.get("triggerprice") else None,
                validity=params.get("duration", "DAY").upper(),
            )
            return _ok(order_id)
        except Exception as exc:
            return _failed(exc)

    def cancel_order(self, order_id: str, variety: str = "NORMAL"):
        try:
            return _ok(self.smart_api.cancel_order(self._variety(variety), order_id))
        except Exception as exc:
            return _failed(exc)

    @staticmethod
    def _variety(value: str) -> str:
        return {"NORMAL": "regular", "REGULAR": "regular", "AMO": "amo", "STOPLOSS": "co"}.get(value.upper(), value.lower())

    @staticmethod
    def _product(value: str) -> str:
        return {"DELIVERY": "CNC", "INTRADAY": "MIS", "CARRYFORWARD": "NRML"}.get(value.upper(), value.upper())

    @staticmethod
    def _order_type(value: str) -> str:
        return {"STOPLOSS_LIMIT": "SL", "STOPLOSS_MARKET": "SL-M"}.get(value.upper(), value.upper())
