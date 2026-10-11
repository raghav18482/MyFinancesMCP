"""Tests for how fundamentals survive Yahoo refusing the server.

Covers the incident of 2026-10-11 (every stock "No data found" in production):

* a 401/429 that yfinance hides behind an empty ``info`` is surfaced with its
  real cause, never reported as "no data";
* a rate limit is not retried, and opens a breaker so the server stops
  digging the hole deeper;
* the Postgres last-known-good store is read first and served, dated, when
  Yahoo fails;
* the API answers no-data with a 200 note and a failure with a 503 carrying
  ``cause`` and a real ``Retry-After``.

yfinance and the database are faked; nothing touches the network. Run
directly (``python tests/test_fundamental_service.py``) or under pytest.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from yfinance.exceptions import YFRateLimitError

import services.fundamental_service as fs
from services import fundamental_store, yahoo_gateway as gw
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
    """Stands in for the curl_cffi HTTPError yfinance's requests raise."""


class Hidden:
    """An ``info`` answer for which yfinance swallowed an HTTP error."""

    def __init__(self, message: str):
        self.message = message


class Yahoo:
    """What the fake Yahoo answers. ``info`` may be a dict, an exception or Hidden."""

    def __init__(self, info, probe):
        self.info = info
        self.probe = probe
        self.info_calls = 0
        self.probe_calls = 0


@contextlib.contextmanager
def fake_yahoo(info, probe=None, stored=None):
    """Fake yfinance, the quote probe and the store; no pacing; clean state."""
    yahoo = Yahoo(info, probe)
    store: dict = dict(stored or {})

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
            if isinstance(yahoo.info, Hidden):
                # What yfinance does with hide_exceptions: log, return empty.
                logging.getLogger("yfinance").error(yahoo.info.message)
                return {}
            return yahoo.info

    class FakeYfData:
        def get_raw_json(self, url, params=None, timeout=30):
            yahoo.probe_calls += 1
            if isinstance(yahoo.probe, Exception):
                raise yahoo.probe
            return yahoo.probe

    def load(symbol):
        return store.get(symbol)

    def save(symbol, result):
        store[symbol] = (result, datetime.now(timezone.utc))
        return True

    saved = (fs.yf.Ticker, fs.YfData, fs._RETRY_BACKOFF, pit.write_snapshot,
             fundamental_store.load, fundamental_store.save, gw._MIN_INTERVAL, dict(fs._cache))
    fs.yf.Ticker = FakeTicker
    fs.YfData = FakeYfData
    fs._RETRY_BACKOFF = 0
    pit.write_snapshot = lambda *a, **k: False
    fundamental_store.load = load
    fundamental_store.save = save
    gw._MIN_INTERVAL = 0
    gw.reset_for_tests()
    fs._cache.clear()
    try:
        yield yahoo, store
    finally:
        (fs.yf.Ticker, fs.YfData, fs._RETRY_BACKOFF, pit.write_snapshot,
         fundamental_store.load, fundamental_store.save, gw._MIN_INTERVAL) = saved[:7]
        gw.reset_for_tests()
        fs._cache.clear()
        fs._cache.update(saved[7])


def _quote_response(*results):
    return {"quoteResponse": {"result": list(results), "error": None}}


def _snapshot(days_old: float):
    fetched = datetime.now(timezone.utc) - timedelta(days=days_old)
    data = {"symbol": "SUZLON-EQ", "valuation": {"pe_ratio": 15.0}, "health": {}, "error": None}
    return {"SUZLON-EQ": (data, fetched)}


# ── Success and no-data ────────────────────────────────────────────────────


def test_a_quote_is_live_data_cached_and_stored():
    with fake_yahoo(QUOTE) as (yahoo, store):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["error"] is None and "retryable" not in r, r
        assert r["source"] == "live" and r["stale"] is False and r["as_of"].endswith("Z")
        assert r["valuation"]["pe_ratio"] == 16.49 and r["health"]["roe"] == 42.0
        assert yahoo.probe_calls == 0, "a good info needs no second request"
        assert "SUZLON-EQ" in store, "a success becomes the last-known-good copy"
        fs.get_stock_fundamentals("SUZLON-EQ")
        assert yahoo.info_calls == 1, "the second call must be a cache hit"
        assert gw.status()["last_success_at"] is not None
    print("ok  a quote is live data, cached in memory and stored in Postgres")


def test_a_symbol_yahoo_does_not_have_is_no_data_not_retryable():
    # yfinance turns Yahoo's 404 into an info with only trailingPegRatio in it.
    with fake_yahoo({"trailingPegRatio": None}, probe=_quote_response()) as (yahoo, store):
        r = fs.get_stock_fundamentals("NOTONYAHOO-EQ")
        assert r["error"] == "No data found for NOTONYAHOO.NS", r
        assert not r.get("retryable")
        assert yahoo.info_calls == 1, "a definite 'no data' is not retried"
        assert "fundamental:NOTONYAHOO-EQ" not in fs._cache and not store
    print("ok  Yahoo saying 'no such symbol' is a calm no-data, not retried, not stored")


def test_a_hidden_404_is_no_data():
    with fake_yahoo(Hidden("404 Client Error: Not Found for url: .../quoteSummary/X.NS")) as (y, _):
        r = fs.get_stock_fundamentals("X-EQ")
        assert r["error"] == "No data found for X.NS" and not r.get("retryable"), r
        assert y.probe_calls == 0
    print("ok  a 404 yfinance hid is no-data")


def test_a_listed_symbol_without_a_price_is_no_data():
    probe = _quote_response({"symbol": "SUSPENDED.NS", "regularMarketPrice": None})
    with fake_yahoo({}, probe=probe):
        r = fs.get_stock_fundamentals("SUSPENDED-EQ")
        assert r["error"] == "No data found for SUSPENDED.NS" and not r.get("retryable"), r
    print("ok  a symbol Yahoo lists but cannot price is no-data, not a failure")


# ── The incident: Yahoo refusing the server ────────────────────────────────


def test_a_hidden_401_is_reported_as_blocked_not_no_data():
    """The production failure: yfinance hid a 401 and returned an empty info."""
    hidden = Hidden("401 Client Error: Unauthorized for url: .../quoteSummary/SUZLON.NS "
                    '{"finance":{"error":{"code":"Unauthorized","description":"Invalid Crumb"}}}')
    with fake_yahoo(hidden) as (yahoo, store):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["retryable"] is True and r["cause"] == gw.BLOCKED, r
        assert "No data found" not in r["error"]
        assert yahoo.probe_calls == 0, "the hidden error already says why"
        assert yahoo.info_calls == 1, "a refusal is not retried"
        assert r["retry_after"] >= 1 and gw.status()["breaker_open"]
        assert "Invalid Crumb" in gw.status()["last_failure_detail"]
        assert "fundamental:SUZLON-EQ" not in fs._cache and not store
    print("ok  a 401 hidden behind an empty info is 'blocked', with Yahoo's own words")


def test_a_rate_limit_is_not_retried_and_opens_the_breaker():
    with fake_yahoo(YFRateLimitError()) as (yahoo, _):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["retryable"] is True and r["cause"] == gw.RATE_LIMITED, r
        assert yahoo.info_calls == 1, "retrying a 429 only lengthens the ban"
        # While the breaker is open, nobody calls Yahoo at all.
        r2 = fs.get_stock_fundamentals("TCS-EQ")
        assert r2["retryable"] is True and yahoo.info_calls == 1, r2
        assert gw.status()["short_circuited"] >= 1
    print("ok  a 429 is not retried, and the breaker stops further Yahoo calls")


def test_the_breaker_cooldown_doubles_and_caps():
    gw.reset_for_tests()
    first = gw.record_failure(gw.RATE_LIMITED, "429")
    second = gw.record_failure(gw.RATE_LIMITED, "429")
    assert second == 2 * first, (first, second)
    for _ in range(20):
        last = gw.record_failure(gw.BLOCKED, "403")
    assert last == gw._COOLDOWN_MAX
    gw.record_success()
    assert not gw.status()["breaker_open"]
    gw.reset_for_tests()
    print("ok  the cooldown doubles on repeats, caps at 30 minutes, and resets on success")


def test_an_http_error_hidden_with_no_log_is_caught_by_the_probe():
    with fake_yahoo({}, probe=HTTPError("HTTP Error 403: Forbidden")) as (yahoo, _):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["retryable"] is True and r["cause"] == gw.BLOCKED, r
        assert yahoo.probe_calls == 1
    print("ok  a refusal only the quote probe sees is 'blocked'")


def test_an_empty_info_for_a_quoted_symbol_is_blocked():
    probe = _quote_response({"symbol": "SUZLON.NS", "regularMarketPrice": 37.6})
    with fake_yahoo({}, probe=probe):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["retryable"] is True and r["cause"] == gw.BLOCKED, r
    print("ok  Yahoo quoting a symbol whose info came back empty is 'blocked'")


def test_a_timeout_is_retried_once_and_does_not_open_the_breaker():
    with fake_yahoo(HTTPError("Operation timed out after 30001 milliseconds")) as (yahoo, _):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["retryable"] is True and r["cause"] == gw.TIMEOUT, r
        assert yahoo.info_calls == fs._INFO_ATTEMPTS
        assert not gw.status()["breaker_open"], "a slow answer is not a ban"
    print("ok  a timeout is retried once, reported, and does not trip the breaker")


# ── Last-known-good store ──────────────────────────────────────────────────


def test_a_fresh_snapshot_is_served_without_calling_yahoo():
    with fake_yahoo(QUOTE, stored=_snapshot(days_old=0.2)) as (yahoo, _):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["source"] == "store" and r["valuation"]["pe_ratio"] == 15.0, r
        assert yahoo.info_calls == 0, "a snapshot younger than a day needs no Yahoo call"
    print("ok  a snapshot under a day old is served with no Yahoo request")


def test_a_failure_serves_the_last_good_figures_marked_stale():
    with fake_yahoo(YFRateLimitError(), stored=_snapshot(days_old=9)) as (yahoo, _):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["source"] == "stale" and r["stale"] is True, r
        assert r["stale_reason"] == gw.RATE_LIMITED and r["as_of"]
        assert r["valuation"]["pe_ratio"] == 15.0 and not r.get("retryable")
        assert yahoo.info_calls == 1, "an old snapshot is refreshed when possible"
        assert fs._cache["fundamental:SUZLON-EQ"]["expires"] - fs.time.time() <= fs._STALE_CACHE_TTL
    print("ok  when Yahoo fails, the last good figures are served with their date")


def test_a_snapshot_too_old_is_not_served():
    with fake_yahoo(YFRateLimitError(), stored=_snapshot(days_old=60)):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["retryable"] is True and r.get("source") != "stale", r
    print("ok  figures older than 45 days are not passed off as current")


def test_recovery_after_the_cooldown_fetches_fresh_data():
    with fake_yahoo(YFRateLimitError()) as (yahoo, _):
        assert fs.get_stock_fundamentals("SUZLON-EQ")["retryable"] is True
        gw._state["open_until"] = 0  # the cooldown has passed
        yahoo.info = QUOTE
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["source"] == "live" and r["current_price"] == 37.6, r
        assert not gw.status()["breaker_open"]
    print("ok  after the cooldown, the next request fetches fresh data")


# ── Gateway ────────────────────────────────────────────────────────────────


def test_classify_maps_yahoo_answers_to_causes():
    cases = {
        "401 Client Error: Unauthorized for url": gw.BLOCKED,
        "403 Forbidden": gw.BLOCKED,
        '{"description":"Invalid Crumb"}': gw.BLOCKED,
        "429 Too Many Requests": gw.RATE_LIMITED,
        "404 Client Error: Not Found": gw.NOT_FOUND,
        "502 Bad Gateway": gw.UPSTREAM,
        "Operation timed out after 30000 ms": gw.TIMEOUT,
        "Could not resolve host: query1.finance.yahoo.com": gw.NETWORK,
    }
    for text, cause in cases.items():
        assert gw.classify(text) == cause, (text, gw.classify(text))
    assert gw.classify(YFRateLimitError()) == gw.RATE_LIMITED
    print("ok  Yahoo's answers map to stable cause codes")


def test_pacing_spaces_request_starts():
    gw.reset_for_tests()
    saved = gw._MIN_INTERVAL
    gw._MIN_INTERVAL = 0.05
    try:
        start = fs.time.time()
        for _ in range(4):
            gw.call(lambda: None, what="test", symbol="X")
        assert fs.time.time() - start >= 0.14, "four starts need three intervals"
    finally:
        gw._MIN_INTERVAL = saved
        gw.reset_for_tests()
    print("ok  request starts are spaced process-wide")


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
          "retryable": True, "cause": "rate_limited", "retry_after": 240,
          "valuation": {}, "health": {}, "score": None}


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
            assert res.json() == {"error": FAILED["error"], "retryable": True, "cause": "rate_limited"}
            assert res.headers.get("retry-after") == "240"
            summary = h.http.post("/api/research/fundamental/summary", json={"symbol": "X-EQ"})
            assert summary.status_code == 503 and summary.json()["retryable"] is True, summary.text
    print("ok  the API answers no-data with a 200 note and a Yahoo failure with a 503")


def test_data_health_reports_the_gateway_and_store():
    from tests.test_spa_api import signed_in

    with signed_in() as h:
        gw.reset_for_tests()
        gw.record_failure(gw.BLOCKED, "info SUZLON.NS: 401 Unauthorized")
        res = h.http.get("/api/research/data-health")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["yahoo"]["breaker_open"] is True
        assert body["yahoo"]["last_failure_cause"] == gw.BLOCKED
        assert "fundamentals_store" in body and "proxy_configured" in body["yahoo"]
        gw.reset_for_tests()
    print("ok  /api/research/data-health shows the cause in production")


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
    print(f"\n{passed} tests passed")
