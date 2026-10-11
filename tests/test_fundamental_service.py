"""Tests for how the fundamentals fetch reports Yahoo's answers.

Two kinds of empty must stay distinguishable: Yahoo *has no data* for a symbol
(a calm "not available" note in the apps) and Yahoo *failed* — rate limited,
blocked, timed out — which must not be cached and must reach the API as a
retryable 503. yfinance hides HTTP errors behind an empty ``info`` by default,
so the service checks an empty ``info`` against the quote endpoint; these tests
drive both paths with yfinance mocked, offline.

Run directly (``python tests/test_fundamental_service.py``) or under pytest.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from yfinance.exceptions import YFRateLimitError

import services.fundamental_service as fs
from services.marketstore import pit

QUOTE = {
    "regularMarketPrice": 37.6,
    "currentPrice": 37.6,
    "longName": "Suzlon Energy Limited",
    "sector": "Industrials",
    "industry": "Specialty Industrial Machinery",
    "marketCap": 515_000_000_000,
    "trailingPE": 16.49,
    "priceToBook": 9.1,
    "returnOnEquity": 0.42,
    "debtToEquity": 2.1,
}


class HTTPError(Exception):
    """Stands in for the curl_cffi HTTPError yfinance's quote request raises."""


class Yahoo:
    """What the fake Yahoo answers. ``info`` may be a dict or an exception."""

    def __init__(self, info, probe):
        self.info = info
        self.probe = probe
        self.info_calls = 0
        self.probe_calls = 0


@contextlib.contextmanager
def fake_yahoo(info, probe=None):
    """Replace ``yf.Ticker`` and the quote probe; restore and clear the cache after."""
    yahoo = Yahoo(info, probe)

    class FakeTicker:
        financials = pd.DataFrame()
        balance_sheet = pd.DataFrame()
        major_holders = pd.DataFrame()

        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            yahoo.info_calls += 1
            if isinstance(yahoo.info, Exception):
                raise yahoo.info
            return yahoo.info

    class FakeYfData:
        def get_raw_json(self, url, params=None, timeout=30):
            yahoo.probe_calls += 1
            if isinstance(yahoo.probe, Exception):
                raise yahoo.probe
            return yahoo.probe

    saved = (fs.yf.Ticker, fs.YfData, fs._RETRY_BACKOFF, pit.write_snapshot, dict(fs._cache))
    fs.yf.Ticker = FakeTicker
    fs.YfData = FakeYfData
    fs._RETRY_BACKOFF = 0
    pit.write_snapshot = lambda *a, **k: False
    fs._cache.clear()
    try:
        yield yahoo
    finally:
        fs.yf.Ticker, fs.YfData, fs._RETRY_BACKOFF, pit.write_snapshot = saved[:4]
        fs._cache.clear()
        fs._cache.update(saved[4])


def _quote_response(*results):
    return {"quoteResponse": {"result": list(results), "error": None}}


@contextlib.contextmanager
def captured_warnings():
    records: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Grab(level=logging.WARNING)
    fs.logger.addHandler(handler)
    try:
        yield records
    finally:
        fs.logger.removeHandler(handler)


# ── Service ────────────────────────────────────────────────────────────────


def test_a_quote_is_a_success_and_is_cached():
    with fake_yahoo(QUOTE) as yahoo:
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["error"] is None and "retryable" not in r, r
        assert r["yf_symbol"] == "SUZLON.NS" and r["current_price"] == 37.6
        assert r["valuation"]["pe_ratio"] == 16.49 and r["health"]["roe"] == 42.0
        assert yahoo.probe_calls == 0, "a good info needs no second request"
        fs.get_stock_fundamentals("SUZLON-EQ")
        assert yahoo.info_calls == 1, "the second call must be a cache hit"
    print("ok  a quote comes back as data, unprobed, and is cached")


def test_a_symbol_yahoo_does_not_have_is_no_data_not_retryable():
    # yfinance turns Yahoo's 404 into an info with only trailingPegRatio in it.
    with fake_yahoo({"trailingPegRatio": None}, probe=_quote_response()) as yahoo:
        r = fs.get_stock_fundamentals("NOTONYAHOO-EQ")
        assert r["error"] == "No data found for NOTONYAHOO.NS", r
        assert not r.get("retryable")
        assert r["valuation"] == {} and r["health"] == {}
        assert yahoo.info_calls == 1, "a definite 'no data' is not retried"
        assert "fundamental:NOTONYAHOO-EQ" not in fs._cache
    print("ok  Yahoo saying 'no such symbol' is a calm no-data, not retried")


def test_a_listed_symbol_without_a_price_is_no_data():
    probe = _quote_response({"symbol": "SUSPENDED.NS", "regularMarketPrice": None})
    with fake_yahoo({}, probe=probe):
        r = fs.get_stock_fundamentals("SUSPENDED-EQ")
        assert r["error"] == "No data found for SUSPENDED.NS" and not r.get("retryable"), r
    print("ok  a symbol Yahoo lists but cannot price is no-data, not a failure")


def test_a_rate_limit_is_retried_then_reported_retryable_and_logged():
    with fake_yahoo(YFRateLimitError()) as yahoo, captured_warnings() as logs:
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["retryable"] is True, r
        assert "No data found" not in r["error"], "a failure must not read as no-data"
        assert yahoo.info_calls == fs._INFO_ATTEMPTS
        assert "fundamental:SUZLON-EQ" not in fs._cache, "a failure must never be cached"
    text = "\n".join(rec.getMessage() for rec in logs)
    assert "YFRateLimitError" in text and "Too Many Requests" in text, text
    print("ok  a 429 is retried, reported retryable, uncached, and its cause logged")


def test_an_http_error_swallowed_by_yfinance_is_caught_by_the_probe():
    # A 401/403 makes yfinance return an empty info; only the probe sees the error.
    with fake_yahoo({}, probe=HTTPError("HTTP Error 401: Unauthorized")) as yahoo, \
            captured_warnings() as logs:
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["retryable"] is True and "No data found" not in r["error"], r
        assert yahoo.probe_calls == fs._INFO_ATTEMPTS
    assert any("401" in rec.getMessage() for rec in logs), [rec.getMessage() for rec in logs]
    print("ok  an HTTP error hidden behind an empty info is a retryable failure")


def test_an_empty_info_for_a_quoted_symbol_is_a_failure():
    probe = _quote_response({"symbol": "SUZLON.NS", "regularMarketPrice": 37.6})
    with fake_yahoo({}, probe=probe):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["retryable"] is True, r
    print("ok  Yahoo quoting a symbol whose info came back empty is a failure")


def test_a_transient_failure_recovers_on_the_next_request():
    with fake_yahoo(YFRateLimitError()) as yahoo:
        assert fs.get_stock_fundamentals("SUZLON-EQ")["retryable"] is True
        yahoo.info = QUOTE
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["error"] is None and r["current_price"] == 37.6, r
    print("ok  Retry after a failure fetches fresh data instead of a cached error")


# ── API ────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def research_returns(result):
    import web.routers.research as research

    saved = research.get_stock_fundamentals
    research.get_stock_fundamentals = lambda symbol: dict(result, symbol=symbol)
    old_key = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = "server-key"
    try:
        yield
    finally:
        research.get_stock_fundamentals = saved
        if old_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = old_key


NO_DATA = {"error": "No data found for X.NS", "valuation": {}, "health": {}, "score": None}
FAILED = {"error": "Couldn't reach Yahoo Finance for X.NS. Please try again shortly.",
          "retryable": True, "valuation": {}, "health": {}, "score": None}


def test_api_no_data_is_a_200_note_and_failure_is_a_retryable_503():
    from tests.test_spa_api import signed_in

    with signed_in() as h:
        with research_returns(NO_DATA):
            res = h.http.get("/api/research/fundamental", params={"symbol": "X-EQ"})
            assert res.status_code == 200 and res.json()["error"] == NO_DATA["error"], res.text
            summary = h.http.post("/api/research/fundamental/summary", json={"symbol": "X-EQ"})
            assert summary.status_code == 400 and "retryable" not in summary.json(), summary.text
        with research_returns(FAILED):
            res = h.http.get("/api/research/fundamental", params={"symbol": "X-EQ"})
            assert res.status_code == 503, res.text
            assert res.json() == {"error": FAILED["error"], "retryable": True}
            assert res.headers.get("retry-after")
            summary = h.http.post("/api/research/fundamental/summary", json={"symbol": "X-EQ"})
            assert summary.status_code == 503 and summary.json()["retryable"] is True, summary.text
    print("ok  the API answers no-data with a 200 note and a Yahoo failure with a 503")


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
    print(f"\n{passed} tests passed")
