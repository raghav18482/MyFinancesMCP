"""
Real-time market data relay using Angel One SmartWebSocket (V1).

Maintains a single WebSocket connection per Angel session and fans out
normalized tick data to registered asyncio callbacks (browser WS handlers).
Falls back to REST LTP polling when the feed is unavailable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class NormalizedTick:
    symbol_token: str
    ltp: float
    volume: int
    ts: float
    bid: float
    ask: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol_token": self.symbol_token,
            "ltp": self.ltp,
            "volume": self.volume,
            "ts": self.ts,
            "bid": self.bid,
            "ask": self.ask,
        }


TickCallback = Callable[[NormalizedTick], Any]


class _FeedConnection:
    """Wraps SmartWebSocket in a background thread with auto-reconnect."""

    def __init__(self, feed_token: str, client_code: str):
        self._feed_token = feed_token
        self._client_code = client_code
        self._ws: Any = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._callbacks: dict[str, list[TickCallback]] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            if self._ws and hasattr(self._ws, "ws") and self._ws.ws:
                self._ws.ws.close()
        except Exception:
            pass

    def subscribe(self, token: str, callback: TickCallback) -> None:
        with self._lock:
            if token not in self._callbacks:
                self._callbacks[token] = []
            self._callbacks[token].append(callback)
        self._ws_subscribe(token)

    def unsubscribe(self, token: str, callback: TickCallback) -> None:
        with self._lock:
            cbs = self._callbacks.get(token, [])
            if callback in cbs:
                cbs.remove(callback)
            if not cbs:
                self._callbacks.pop(token, None)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return sum(len(cbs) for cbs in self._callbacks.values())

    def _ws_subscribe(self, token: str) -> None:
        if self._ws is None:
            return
        try:
            channel = f"nse_cm|{token}"
            self._ws.subscribe("mw", channel)
        except Exception as e:
            logger.warning("WS subscribe failed for %s: %s", token, e)

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._connect_and_listen()
            except Exception as e:
                logger.warning("Feed connection error, reconnecting in 5s: %s", e)
            if self._running:
                time.sleep(5)

    def _connect_and_listen(self) -> None:
        from SmartApi.smartApiWebsocket import SmartWebSocket

        self._ws = SmartWebSocket(self._feed_token, self._client_code)

        original_on_message = None

        def on_message(ws, message):
            self._handle_message(message)

        def on_open(ws):
            logger.info("Angel WebSocket connected")
            with self._lock:
                tokens = list(self._callbacks.keys())
            for t in tokens:
                self._ws_subscribe(t)

        def on_error(ws, error):
            logger.warning("Angel WebSocket error: %s", error)

        def on_close(ws, *args):
            logger.info("Angel WebSocket closed")

        self._ws.ws = None
        import websocket
        ws_app = websocket.WebSocketApp(
            self._ws.root,
            on_message=on_message,
            on_open=on_open,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws.ws = ws_app
        ws_app.run_forever(ping_interval=30, ping_timeout=10)

    def _handle_message(self, message: Any) -> None:
        try:
            if isinstance(message, str):
                data = json.loads(message)
            else:
                logger.debug("Binary tick received (len=%d), skipping parse", len(message))
                return

            token = str(data.get("tk", data.get("token", "")))
            ltp = float(data.get("ltp", data.get("last_price", 0)))
            volume = int(data.get("v", data.get("volume", 0)))
            bid = float(data.get("bp1", data.get("best_bid_price", 0)))
            ask = float(data.get("sp1", data.get("best_ask_price", 0)))

            tick = NormalizedTick(
                symbol_token=token,
                ltp=ltp,
                volume=volume,
                ts=time.time(),
                bid=bid,
                ask=ask,
            )

            with self._lock:
                callbacks = list(self._callbacks.get(token, []))

            for cb in callbacks:
                try:
                    cb(tick)
                except Exception as e:
                    logger.debug("Tick callback error: %s", e)

        except Exception as e:
            logger.debug("Tick parse error: %s", e)


class MarketFeedRelay:
    """
    Per-session feed manager. One _FeedConnection per Angel session,
    browser WS handlers register/unregister per symbol token.
    """

    def __init__(self) -> None:
        self._feeds: dict[str, _FeedConnection] = {}
        self._lock = threading.Lock()

    def get_or_start_feed(self, session_id: str, feed_token: str, client_code: str) -> _FeedConnection:
        with self._lock:
            if session_id not in self._feeds:
                conn = _FeedConnection(feed_token, client_code)
                conn.start()
                self._feeds[session_id] = conn
                logger.info("Started market feed for session %s", session_id[:8])
            return self._feeds[session_id]

    def stop_feed(self, session_id: str) -> None:
        with self._lock:
            conn = self._feeds.pop(session_id, None)
        if conn:
            conn.stop()

    def stop_all(self) -> None:
        with self._lock:
            feeds = list(self._feeds.values())
            self._feeds.clear()
        for f in feeds:
            f.stop()


feed_relay = MarketFeedRelay()


def _extract_ltp(result: dict) -> float | None:
    """Extract LTP from Angel's ltpData response, handling nesting variants."""
    if not result.get("status"):
        return None
    data = result.get("data")
    if data is None:
        return None
    if isinstance(data, dict):
        if "ltp" in data:
            return float(data["ltp"])
        fetched = data.get("fetched") or data.get("data")
        if isinstance(fetched, list) and fetched:
            return float(fetched[0].get("ltp", 0))
        if isinstance(fetched, dict) and "ltp" in fetched:
            return float(fetched["ltp"])
    return None


async def poll_ltp_fallback(
    client: Any,
    exchange: str,
    tradingsymbol: str,
    symboltoken: str,
    interval: float = 2.0,
) -> Any:
    """Async generator that polls LTP and yields NormalizedTick objects."""
    while True:
        try:
            result = await asyncio.to_thread(
                client.get_ltp, exchange, tradingsymbol, symboltoken
            )
            ltp = _extract_ltp(result)
            if ltp and ltp > 0:
                yield NormalizedTick(
                    symbol_token=symboltoken,
                    ltp=ltp,
                    volume=0,
                    ts=time.time(),
                    bid=0.0,
                    ask=0.0,
                )
            else:
                result2 = await asyncio.to_thread(
                    client.get_market_data, "LTP", {exchange: [symboltoken]}
                )
                fetched = (result2.get("data") or {}).get("fetched", [])
                if fetched:
                    ltp2 = float(fetched[0].get("ltp", 0))
                    if ltp2 > 0:
                        yield NormalizedTick(
                            symbol_token=symboltoken,
                            ltp=ltp2,
                            volume=0,
                            ts=time.time(),
                            bid=0.0,
                            ask=0.0,
                        )
        except Exception as e:
            logger.debug("LTP poll error for %s: %s", tradingsymbol, e)
        await asyncio.sleep(interval)
