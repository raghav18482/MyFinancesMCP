"""Tests for services.realtime_feed.

Covers the SmartWebSocketV2 tick decoding, the REST polling fallback, and the
handover between them. No broker credentials or network access required.

Run directly (``python tests/test_realtime_feed.py``) or under pytest.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.realtime_feed as rf
from services.realtime_feed import (
    NormalizedTick,
    Subscription,
    _tick_from_v2,
    exchange_type,
    stream_ticks,
)

# A QUOTE packet as SmartWebSocketV2 hands it over after binary unpacking.
# Prices are integer paise.
QUOTE_PACKET = {
    "subscription_mode": 2,
    "subscription_mode_val": "QUOTE",
    "exchange_type": 1,
    "token": "2885",
    "last_traded_price": 142535,
    "volume_trade_for_the_day": 8412300,
    "open_price_of_the_day": 141000,
}


def test_exchange_type_mapping():
    assert exchange_type("NSE") == 1
    assert exchange_type("BSE") == 3
    assert exchange_type("nse") == 1
    assert exchange_type("UNKNOWN") == 1  # defaults to NSE cash
    assert exchange_type("") == 1


def test_quote_packet_scales_paise_to_rupees():
    tick = _tick_from_v2(QUOTE_PACKET)
    assert tick is not None
    assert abs(tick.ltp - 1425.35) < 1e-9
    assert tick.volume == 8412300
    assert tick.source == "ws"
    # QUOTE carries no best bid/ask
    assert tick.bid == 0.0 and tick.ask == 0.0


def test_snap_quote_packet_fills_bid_and_ask():
    snap = dict(
        QUOTE_PACKET,
        best_5_buy_data=[{"price": 142500}],
        best_5_sell_data=[{"price": 142560}],
    )
    tick = _tick_from_v2(snap)
    assert tick is not None
    assert tick.bid == 1425.00
    assert tick.ask == 1425.60


def test_malformed_packets_are_rejected_not_raised():
    assert _tick_from_v2({}) is None
    assert _tick_from_v2({"token": "", "last_traded_price": 100}) is None
    assert _tick_from_v2({"token": "2885", "last_traded_price": 0}) is None
    assert _tick_from_v2({"token": "2885"}) is None


class _NoFeedClient:
    """Lacks a jwt, so the relay cannot open a socket: poll path only."""

    api_key = "k"
    client_id = "C1"
    feed_token = None
    auth_token = None

    def __init__(self):
        self.poll_calls = 0

    def get_ltp(self, exchange, tradingsymbol, symboltoken):
        self.poll_calls += 1
        return {"status": True, "data": {"ltp": 1000 + self.poll_calls}}


def test_polling_fallback_when_feed_unavailable():
    async def run():
        client = _NoFeedClient()
        subs = [Subscription("2885", "RELIANCE", "NSE")]
        agen = stream_ticks(client, "sess-poll", subs, poll_interval=0.05, stale_after=0.2)

        got = []
        async for tick in agen:
            got.append(tick)
            if len(got) == 3:
                break
        await agen.aclose()

        assert [t.source for t in got] == ["poll"] * 3
        assert [t.ltp for t in got] == [1001, 1002, 1003]
        assert rf.feed_relay.get("sess-poll") is None

        # closing the stream must stop the poller
        frozen = client.poll_calls
        await asyncio.sleep(0.25)
        assert client.poll_calls == frozen

    asyncio.run(run())


class _FakeConn:
    """Stands in for a live _FeedConnection."""

    def __init__(self, deliver: bool = True):
        self.deliver = deliver
        self.cbs: dict[str, list] = {}
        self.unsubscribed: list[str] = []
        self._last: dict[str, float] = {}
        self._stop = threading.Event()

    def subscribe(self, token, exch_type, cb):
        self.cbs.setdefault(token, []).append(cb)
        if self.deliver:
            threading.Thread(target=self._pump, args=(token,), daemon=True).start()

    def unsubscribe(self, token, cb):
        self.unsubscribed.append(token)
        self._stop.set()

    def last_tick_age(self, token):
        ts = self._last.get(token)
        return float("inf") if ts is None else time.time() - ts

    def _pump(self, token):
        n = 0
        while not self._stop.is_set():
            n += 1
            self._last[token] = time.time()
            for cb in list(self.cbs.get(token, [])):
                cb(NormalizedTick(token, 2000.0 + n, 500, time.time(), 0.0, 0.0, "ws"))
            time.sleep(0.04)


class _LiveFeedClient(_NoFeedClient):
    feed_token = "f"
    auth_token = "jwt"


def test_websocket_takes_over_and_releases_the_poller(monkeypatch=None):
    async def run():
        conn = _FakeConn(deliver=True)
        original = rf.feed_relay.get_or_start_feed
        rf.feed_relay.get_or_start_feed = lambda sid, client: conn
        try:
            client = _LiveFeedClient()
            subs = [Subscription("2885", "RELIANCE", "NSE")]
            agen = stream_ticks(client, "sess-ws", subs, poll_interval=0.05, stale_after=0.15)

            ws_ticks = []
            async for tick in agen:
                if tick.source == "ws":
                    ws_ticks.append(tick)
                if len(ws_ticks) >= 12:
                    break

            # volume only survives the websocket path
            assert all(t.volume == 500 for t in ws_ticks)

            frozen = client.poll_calls
            await asyncio.sleep(0.3)
            assert client.poll_calls == frozen, "poller still running after ws took over"

            await agen.aclose()
            assert "2885" in conn.unsubscribed
        finally:
            rf.feed_relay.get_or_start_feed = original

    asyncio.run(run())


def test_silent_websocket_falls_back_to_polling():
    async def run():
        conn = _FakeConn(deliver=False)  # subscribes but never ticks
        original = rf.feed_relay.get_or_start_feed
        rf.feed_relay.get_or_start_feed = lambda sid, client: conn
        try:
            client = _LiveFeedClient()
            subs = [Subscription("2885", "RELIANCE", "NSE")]
            agen = stream_ticks(client, "sess-dead", subs, poll_interval=0.05, stale_after=0.1)

            got = []
            async for tick in agen:
                got.append(tick)
                if len(got) == 3:
                    break
            await agen.aclose()
            assert [t.source for t in got] == ["poll"] * 3
        finally:
            rf.feed_relay.get_or_start_feed = original

    asyncio.run(run())


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
