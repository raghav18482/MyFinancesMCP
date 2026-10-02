"""Tests for the /api/market/stream Server-Sent Events endpoint.

Drives the StreamingResponse generator directly rather than through
fastapi.testclient, because TestClient buffers endless response bodies and
would block forever on a live stream.

Run directly (``python tests/test_market_stream.py``) or under pytest.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.routers import market


class _FakeClient:
    """No feed credentials, so ticks arrive over the REST poll path."""

    api_key = "k"
    client_id = "C1"
    feed_token = None
    auth_token = None

    def __init__(self):
        self.n = 0

    def get_ltp(self, exchange, tradingsymbol, symboltoken):
        self.n += 1
        return {"status": True, "data": {"ltp": 1500 + self.n}}

    def search_scrip(self, exchange, text):
        return {"status": True, "data": []}


def _authenticate_as(client):
    market.require_login = lambda request: client
    market.session_id = lambda request: "sess-test"


async def _read_frames(response, count, timeout=10.0):
    """Collect `count` complete SSE frames from a StreamingResponse."""
    frames, buf = [], ""
    it = response.body_iterator.__aiter__()
    while len(frames) < count:
        chunk = await asyncio.wait_for(it.__anext__(), timeout=timeout)
        buf += chunk if isinstance(chunk, str) else chunk.decode()
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            frames.append(frame)
    await response.body_iterator.aclose()
    return frames


def _parse(frame):
    """Split an SSE frame into (event name, decoded data)."""
    lines = frame.splitlines()
    assert lines[0].startswith("event: "), lines
    assert lines[1].startswith("data: "), lines
    return lines[0][len("event: "):], json.loads(lines[1][len("data: "):])


def test_requires_authentication():
    async def run():
        market.require_login = lambda request: None
        resp = await market.api_market_stream(
            request=object(), symbols="RELIANCE", tokens="", exchange="NSE"
        )
        assert resp.status_code == 401

    asyncio.run(run())


def test_rejects_empty_symbol_list():
    async def run():
        _authenticate_as(_FakeClient())
        resp = await market.api_market_stream(
            request=object(), symbols="", tokens="", exchange="NSE"
        )
        assert resp.status_code == 400

    asyncio.run(run())


def test_unresolvable_symbol_returns_404():
    async def run():
        _authenticate_as(_FakeClient())
        resp = await market.api_market_stream(
            request=object(), symbols="NOSUCHSYM", tokens="", exchange="NSE"
        )
        assert resp.status_code == 404

    asyncio.run(run())


def test_sse_headers_disable_buffering():
    async def run():
        _authenticate_as(_FakeClient())
        resp = await market.api_market_stream(
            request=object(), symbols="RELIANCE", tokens="2885", exchange="NSE"
        )
        assert resp.media_type == "text/event-stream"
        headers = {k.lower(): v for k, v in resp.headers.items()}
        # Without this nginx buffers the response and the stream appears to hang.
        assert headers["x-accel-buffering"] == "no"
        assert "no-cache" in headers["cache-control"]
        await resp.body_iterator.aclose()

    asyncio.run(run())


def test_handshake_then_tick_frames():
    async def run():
        _authenticate_as(_FakeClient())
        resp = await market.api_market_stream(
            request=object(), symbols="RELIANCE", tokens="2885", exchange="NSE"
        )
        frames = await _read_frames(resp, 4)

        event, data = _parse(frames[0])
        assert event == "subscribed"
        assert data["symbols"] == [{"tradingsymbol": "RELIANCE", "symboltoken": "2885"}]
        assert data["unresolved"] == []

        for frame in frames[1:]:
            event, tick = _parse(frame)
            assert event == "tick"
            assert tick["symbol_token"] == "2885"
            assert tick["source"] == "poll"
            assert tick["ltp"] > 0

    asyncio.run(run())


def test_symbol_count_is_capped():
    async def run():
        _authenticate_as(_FakeClient())
        n = 30
        resp = await market.api_market_stream(
            request=object(),
            symbols=",".join(f"SYM{i}" for i in range(n)),
            tokens=",".join(str(1000 + i) for i in range(n)),
            exchange="NSE",
        )
        frames = await _read_frames(resp, 1)
        _, data = _parse(frames[0])
        assert len(data["symbols"]) == market._MAX_STREAM_SYMBOLS

    asyncio.run(run())


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
