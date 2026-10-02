"""
Real-time market data relay for browser price streams.

Primary path
    Angel One ``SmartWebSocketV2``. One broker connection per Angel session,
    fanned out to every browser stream that wants a token. V2 unpacks the
    binary tick payload itself, so ticks arrive as dicts (unlike V1, where the
    binary frames had to be parsed by hand and were previously discarded).

Fallback path
    REST ``ltpData`` polling, started per token only when the WebSocket has
    delivered nothing for ``STALE_AFTER`` seconds, and stopped again as soon as
    live ticks resume. The fallback is per token, not global, so one dead
    subscription does not drag the whole feed onto REST.

Public entry point is :func:`stream_ticks`, an async generator consumed by the
SSE endpoint in ``web.routers.market``.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# Angel exchange-type codes used by SmartWebSocketV2 subscriptions.
_EXCHANGE_TYPES = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "CDS": 13}

# V2 quotes integer paise; divide to get rupees.
_PAISE = 100.0

# No WS tick for this long on a token and we start polling it.
STALE_AFTER = 8.0

# Interval for the REST fallback poll.
POLL_INTERVAL = 2.0

# How often the supervisor re-checks staleness.
_SUPERVISE_INTERVAL = 2.0

# Bound the per-stream queue so a stalled browser cannot grow memory forever.
_QUEUE_MAX = 256


@dataclass
class NormalizedTick:
    symbol_token: str
    ltp: float
    volume: int
    ts: float
    bid: float
    ask: float
    source: str = "ws"  # "ws" | "poll"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol_token": self.symbol_token,
            "ltp": self.ltp,
            "volume": self.volume,
            "ts": self.ts,
            "bid": self.bid,
            "ask": self.ask,
            "source": self.source,
        }


TickCallback = Callable[[NormalizedTick], Any]


def exchange_type(exchange: str) -> int:
    return _EXCHANGE_TYPES.get((exchange or "NSE").upper(), 1)


def _tick_from_v2(payload: dict) -> Optional[NormalizedTick]:
    """Build a NormalizedTick from a SmartWebSocketV2 parsed packet."""
    token = str(payload.get("token") or "").strip()
    if not token:
        return None

    ltp = float(payload.get("last_traded_price") or 0) / _PAISE
    if ltp <= 0:
        return None

    volume = int(payload.get("volume_trade_for_the_day") or 0)

    # Best bid/ask only exist in SNAP_QUOTE packets; stay at 0 otherwise.
    bid = ask = 0.0
    for key, setter in (("best_5_buy_data", "bid"), ("best_5_sell_data", "ask")):
        rows = payload.get(key)
        if isinstance(rows, list) and rows:
            price = float(rows[0].get("price") or 0) / _PAISE
            if setter == "bid":
                bid = price
            else:
                ask = price

    return NormalizedTick(
        symbol_token=token,
        ltp=ltp,
        volume=volume,
        ts=time.time(),
        bid=bid,
        ask=ask,
        source="ws",
    )


class _Subscription:
    __slots__ = ("exchange_type", "callbacks", "last_tick_ts")

    def __init__(self, exch_type: int) -> None:
        self.exchange_type = exch_type
        self.callbacks: list[TickCallback] = []
        self.last_tick_ts: float = 0.0


class _FeedConnection:
    """One SmartWebSocketV2 connection, run on a daemon thread."""

    def __init__(self, auth_token: str, api_key: str, client_code: str, feed_token: str):
        self._auth_token = auth_token
        self._api_key = api_key
        self._client_code = client_code
        self._feed_token = feed_token

        self._ws: Any = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = threading.Event()
        self._subs: dict[str, _Subscription] = {}
        self._lock = threading.RLock()
        self._corr_id = f"relay-{int(time.time())}"

    # ── lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name="angel-feed", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            ws = self._ws
        self._connected.clear()
        if ws is not None:
            try:
                ws.close_connection()
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return sum(len(s.callbacks) for s in self._subs.values())

    # ── subscriptions ────────────────────────────────────────────

    def subscribe(self, token: str, exch_type: int, callback: TickCallback) -> None:
        with self._lock:
            sub = self._subs.get(token)
            is_new = sub is None
            if sub is None:
                sub = _Subscription(exch_type)
                self._subs[token] = sub
            sub.callbacks.append(callback)
        if is_new:
            self._ws_subscribe(token, exch_type)

    def unsubscribe(self, token: str, callback: TickCallback) -> None:
        drop = False
        with self._lock:
            sub = self._subs.get(token)
            if sub is None:
                return
            if callback in sub.callbacks:
                sub.callbacks.remove(callback)
            if not sub.callbacks:
                self._subs.pop(token, None)
                drop = True
        if drop:
            self._ws_unsubscribe(token, sub.exchange_type)

    def last_tick_age(self, token: str) -> float:
        """Seconds since the last WS tick for this token; inf if never."""
        with self._lock:
            sub = self._subs.get(token)
            if sub is None or sub.last_tick_ts == 0.0:
                return float("inf")
            return time.time() - sub.last_tick_ts

    def _token_list(self, token: str, exch_type: int) -> list[dict]:
        return [{"exchangeType": exch_type, "tokens": [token]}]

    def _ws_subscribe(self, token: str, exch_type: int) -> None:
        ws = self._ws
        if ws is None or not self._connected.is_set():
            return  # _on_open resubscribes everything
        try:
            ws.subscribe(self._corr_id, ws.QUOTE, self._token_list(token, exch_type))
            logger.debug("Feed subscribed token=%s exch=%s", token, exch_type)
        except Exception as e:
            logger.warning("WS subscribe failed for %s: %s", token, e)

    def _ws_unsubscribe(self, token: str, exch_type: int) -> None:
        ws = self._ws
        if ws is None or not self._connected.is_set():
            return
        try:
            ws.unsubscribe(self._corr_id, ws.QUOTE, self._token_list(token, exch_type))
        except Exception as e:
            logger.debug("WS unsubscribe failed for %s: %s", token, e)

    def _resubscribe_all(self) -> None:
        with self._lock:
            pending = [(t, s.exchange_type) for t, s in self._subs.items()]
        for token, exch in pending:
            self._ws_subscribe(token, exch)

    # ── thread body ──────────────────────────────────────────────

    def _run_loop(self) -> None:
        backoff = 2.0
        while self._running:
            try:
                self._connect_and_listen()
                backoff = 2.0
            except Exception as e:
                logger.warning("Feed connection error: %s", e)
            finally:
                self._connected.clear()
            if self._running:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _connect_and_listen(self) -> None:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        ws = SmartWebSocketV2(
            self._auth_token,
            self._api_key,
            self._client_code,
            self._feed_token,
            max_retry_attempt=0,
        )
        # SmartWebSocketV2 declares ``input_request_dict`` at class level, so
        # every instance in the process shares one dict. With a feed per user
        # session that means one user's subscribed tokens are visible to
        # another user's socket, and ``resubscribe()`` would subscribe them.
        # Shadow it with a per-instance dict.
        ws.input_request_dict = {}
        self._ws = ws

        def on_open(_wsapp):
            self._connected.set()
            logger.info("Angel SmartWebSocketV2 connected (%s)", self._client_code)
            self._resubscribe_all()

        def on_data(_wsapp, message):
            self._handle_tick(message)

        def on_error(reason, detail):
            # SmartWebSocketV2 invokes this as on_error(<reason>, <detail>);
            # neither argument is the websocket app.
            logger.warning("Angel feed error: %s (%s)", reason, detail)
            self._connected.clear()

        def on_close(_wsapp):
            logger.info("Angel feed closed")
            self._connected.clear()

        ws.on_open = on_open
        ws.on_data = on_data
        ws.on_error = on_error
        ws.on_close = on_close

        ws.connect()  # blocks on run_forever until the socket drops

    def _handle_tick(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        tick = _tick_from_v2(message)
        if tick is None:
            return

        with self._lock:
            sub = self._subs.get(tick.symbol_token)
            if sub is None:
                return
            sub.last_tick_ts = tick.ts
            callbacks = list(sub.callbacks)

        for cb in callbacks:
            try:
                cb(tick)
            except Exception as e:
                logger.debug("Tick callback error: %s", e)


class MarketFeedRelay:
    """One :class:`_FeedConnection` per Angel session id."""

    def __init__(self) -> None:
        self._feeds: dict[str, _FeedConnection] = {}
        self._lock = threading.Lock()

    def get_or_start_feed(self, session_id: str, client: Any) -> Optional[_FeedConnection]:
        """Return a started feed, or None when the session lacks feed credentials."""
        auth_token = getattr(client, "auth_token", None)
        feed_token = getattr(client, "feed_token", None)
        api_key = getattr(client, "api_key", None)
        client_code = getattr(client, "client_code", None) or getattr(client, "client_id", None)

        if not all([auth_token, feed_token, api_key, client_code]):
            logger.info("Feed credentials incomplete for %s; REST polling only", session_id[:8])
            return None

        with self._lock:
            conn = self._feeds.get(session_id)
            if conn is None:
                conn = _FeedConnection(auth_token, api_key, client_code, feed_token)
                conn.start()
                self._feeds[session_id] = conn
                logger.info("Started market feed for session %s", session_id[:8])
            return conn

    def get(self, session_id: str) -> Optional[_FeedConnection]:
        with self._lock:
            return self._feeds.get(session_id)

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


# ── REST fallback ────────────────────────────────────────────────


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


async def _fetch_ltp_once(
    client: Any, exchange: str, tradingsymbol: str, symboltoken: str
) -> Optional[NormalizedTick]:
    """One REST LTP read, with the getMarketData variant as a second attempt."""
    try:
        result = await asyncio.to_thread(
            client.get_ltp, exchange, tradingsymbol, symboltoken
        )
        ltp = _extract_ltp(result)
        if not (ltp and ltp > 0):
            result2 = await asyncio.to_thread(
                client.get_market_data, "LTP", {exchange: [symboltoken]}
            )
            fetched = (result2.get("data") or {}).get("fetched", [])
            ltp = float(fetched[0].get("ltp", 0)) if fetched else 0.0
        if ltp and ltp > 0:
            return NormalizedTick(
                symbol_token=symboltoken,
                ltp=float(ltp),
                volume=0,
                ts=time.time(),
                bid=0.0,
                ask=0.0,
                source="poll",
            )
    except Exception as e:
        logger.debug("LTP poll error for %s: %s", tradingsymbol, e)
    return None


# ── unified stream ───────────────────────────────────────────────


@dataclass
class Subscription:
    """One symbol a browser stream wants."""

    symboltoken: str
    tradingsymbol: str
    exchange: str = "NSE"


async def stream_ticks(
    client: Any,
    session_id: str,
    subs: Iterable[Subscription],
    *,
    poll_interval: float = POLL_INTERVAL,
    stale_after: float = STALE_AFTER,
) -> Any:
    """
    Yield :class:`NormalizedTick` for every subscription, preferring the
    WebSocket feed and polling only the tokens it is not delivering.

    Cancelling the consumer unsubscribes everything and stops the pollers.
    """
    subs = list(subs)
    if not subs:
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[NormalizedTick] = asyncio.Queue(maxsize=_QUEUE_MAX)

    def push(tick: NormalizedTick) -> None:
        """Called from the feed thread."""
        def _put() -> None:
            if queue.full():
                try:
                    queue.get_nowait()  # drop oldest
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(tick)

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # loop already closed

    conn = await asyncio.to_thread(feed_relay.get_or_start_feed, session_id, client)

    registered: list[tuple[str, TickCallback]] = []
    if conn is not None:
        for s in subs:
            conn.subscribe(s.symboltoken, exchange_type(s.exchange), push)
            registered.append((s.symboltoken, push))

    pollers: dict[str, asyncio.Task] = {}

    async def poll_token(s: Subscription) -> None:
        while True:
            tick = await _fetch_ltp_once(
                client, s.exchange, s.tradingsymbol, s.symboltoken
            )
            if tick is not None:
                await queue.put(tick)
            await asyncio.sleep(poll_interval)

    def start_poller(s: Subscription) -> None:
        if s.symboltoken in pollers:
            return
        pollers[s.symboltoken] = asyncio.create_task(poll_token(s))
        logger.info("Polling fallback engaged for %s", s.tradingsymbol)

    def stop_poller(token: str, tradingsymbol: str) -> None:
        task = pollers.pop(token, None)
        if task is not None:
            task.cancel()
            logger.info("Polling fallback released for %s", tradingsymbol)

    async def supervise() -> None:
        """Engage or release the REST fallback per token based on WS staleness."""
        while True:
            for s in subs:
                age = conn.last_tick_age(s.symboltoken) if conn else float("inf")
                if age > stale_after:
                    start_poller(s)
                elif s.symboltoken in pollers:
                    stop_poller(s.symboltoken, s.tradingsymbol)
            await asyncio.sleep(_SUPERVISE_INTERVAL)

    # Engage polling immediately; the supervisor releases it once ticks arrive.
    for s in subs:
        start_poller(s)
    supervisor = asyncio.create_task(supervise())

    try:
        while True:
            yield await queue.get()
    finally:
        supervisor.cancel()
        for task in list(pollers.values()):
            task.cancel()
        pollers.clear()
        if conn is not None:
            for token, cb in registered:
                conn.unsubscribe(token, cb)
