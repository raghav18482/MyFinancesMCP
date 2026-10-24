"""Tests for stored fundamentals and the endpoints the scheduled job calls.

Yahoo refuses the production server, so production serves fundamentals from a
Postgres table that a GitHub Actions job fills through
``/api/internal/fundamentals``. These tests cover the store, the service
reading it first, GitHub's identity-token check, and both endpoints, against a
temporary SQLite database with Yahoo and GitHub's keys faked.

Run directly (``python tests/test_fundamental_store.py``) or under pytest.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt
import numpy as np
from cryptography.hazmat.primitives.asymmetric import rsa

import services.fundamental_service as fs
from services import fundamental_store as store
from services import github_oidc

GOOD = {
    "symbol": "SUZLON-EQ",
    "yf_symbol": "SUZLON.NS",
    "company_name": "Suzlon Energy Limited",
    "current_price": 37.6,
    "valuation": {"pe_ratio": 16.49},
    "health": {"roe": 42.0},
    "revenue_trend": [],
    "profit_trend": [],
    "score": {"total": 83},
    "error": None,
}


def _db_engine_module():
    """The ``db.engine`` module; ``db/__init__`` rebinds ``db.engine`` to the Engine."""
    import db  # noqa: F401 — makes sure the module is loaded

    return sys.modules["db.engine"]


@contextlib.contextmanager
def temp_store():
    """Point the store at a fresh SQLite database; also empties the service cache."""
    from sqlmodel import Session, SQLModel, create_engine

    from db import models  # noqa: F401 — registers the tables

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        SQLModel.metadata.create_all(engine)
        saved = _db_engine_module().get_session, dict(fs._cache)
        _db_engine_module().get_session = lambda: Session(engine)
        fs._cache.clear()
        try:
            yield engine
        finally:
            _db_engine_module().get_session = saved[0]
            fs._cache.clear()
            fs._cache.update(saved[1])
            engine.dispose()


@contextlib.contextmanager
def yahoo_must_not_be_called():
    saved = fs.fetch_live

    def refuse(symbol):
        raise AssertionError(f"Yahoo was called for {symbol}")

    fs.fetch_live = refuse
    try:
        yield
    finally:
        fs.fetch_live = saved


@contextlib.contextmanager
def yahoo_answers(result_for):
    """``fetch_live`` replaced by ``result_for(symbol)``; counts the calls."""
    saved = fs.fetch_live
    calls: list[str] = []

    def fake(symbol):
        calls.append(symbol)
        return result_for(symbol)

    fs.fetch_live = fake
    try:
        yield calls
    finally:
        fs.fetch_live = saved


def _refused(symbol):
    r = fs._empty_result(symbol)
    r.update(error="Couldn't reach Yahoo Finance", retryable=True)
    return r


# ── The store ──────────────────────────────────────────────────────────────


def test_symbols_are_stored_under_their_base_nse_symbol():
    assert store.key("SUZLON-EQ") == store.key("suzlon.ns") == store.key(" SUZLON ") == "SUZLON"
    assert store.key("BAJAJ-AUTO-EQ") == "BAJAJ-AUTO" and store.key("M&M-EQ") == "M&M"
    assert store.key("") is None and store.key("DROP TABLE;") is None and store.key("X" * 21) is None
    print("ok  stocks are keyed by base NSE symbol, and junk is not a key")


def test_clean_makes_a_result_json_safe():
    raw = {"a": np.float64(1.5), "b": [np.int64(2), float("nan")], "c": {"d": float("inf")}, "e": "x"}
    out = store.clean(raw)
    assert out == {"a": 1.5, "b": [2, None], "c": {"d": None}, "e": "x"}, out
    assert type(out["b"][0]) is int
    json.dumps(out, allow_nan=False)
    print("ok  numpy values become Python values and NaN/inf become null")


def test_a_saved_result_loads_back_with_its_date():
    with temp_store():
        assert store.save(dict(GOOD, valuation={"pe_ratio": np.float64(16.49)}))
        r = store.load("SUZLON")
        assert r["symbol"] == "SUZLON", "the caller's symbol is echoed back"
        assert r["valuation"]["pe_ratio"] == 16.49 and r["company_name"] == "Suzlon Energy Limited"
        as_of = datetime.fromisoformat(r["as_of"])
        assert abs((datetime.now(timezone.utc) - as_of).total_seconds()) < 60
        assert store.load("TCS-EQ") is None
        assert not store.save(dict(GOOD, error="No data found")), "only successes are stored"
    print("ok  a saved result loads back, under any spelling, with as_of")


def test_a_database_failure_reads_as_nothing_stored():
    def broken():
        raise RuntimeError("database down")

    saved = _db_engine_module().get_session
    _db_engine_module().get_session = broken
    try:
        assert store.load("SUZLON-EQ") is None
        assert store.requested_recently("SUZLON-EQ") is False
        assert store.request("SUZLON-EQ") is False
        assert store.save(GOOD) is False
        assert store.forget_requests(["SUZLON"]) == 0
    finally:
        _db_engine_module().get_session = saved
    print("ok  a database failure never escapes the store's request-path functions")


def test_the_queue_lists_requests_oldest_first_and_expires_old_ones():
    from sqlmodel import Session

    from db.models import FundamentalSnapshot

    now = datetime.now(timezone.utc)
    with temp_store() as engine:
        store.save(GOOD)
        with Session(engine) as s:
            s.add(FundamentalSnapshot(symbol="NEWER", requested_at=now - timedelta(hours=1)))
            s.add(FundamentalSnapshot(symbol="OLDER", requested_at=now - timedelta(hours=5)))
            s.add(FundamentalSnapshot(symbol="ABANDONED", requested_at=now - timedelta(days=8)))
            s.commit()
        q = store.queue(now)
        assert q["wanted"] == ["OLDER", "NEWER"], q
        assert list(q["stored"]) == ["SUZLON"] and q["stored"]["SUZLON"], q
        with Session(engine) as s:
            assert s.get(FundamentalSnapshot, "ABANDONED") is None, "an expired request is deleted"

        assert store.forget_requests(["OLDER-EQ", "SUZLON"]) == 1, "only data-less rows are dropped"
        assert store.queue(now)["wanted"] == ["NEWER"] and store.load("SUZLON")
    print("ok  the queue lists live requests oldest first and drops expired ones")


# ── The service reads the store first ──────────────────────────────────────


def test_a_stored_stock_is_served_without_calling_yahoo():
    with temp_store(), yahoo_must_not_be_called():
        store.save(GOOD)
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["error"] is None and r["valuation"]["pe_ratio"] == 16.49 and r["as_of"], r
        assert r["symbol"] == "SUZLON-EQ"
    print("ok  a stored stock is served from the store, with no Yahoo call")


def test_a_stock_yahoo_refuses_is_queued_and_not_retried_at_once():
    with temp_store(), yahoo_answers(_refused) as calls:
        r = fs.get_stock_fundamentals("TCS-EQ")
        assert r["retryable"] is True and "being fetched" in r["error"], r
        assert store.queue()["wanted"] == ["TCS"]
        again = fs.get_stock_fundamentals("TCS-EQ")
        assert "being fetched" in again["error"] and calls == ["TCS-EQ"], "no second Yahoo call"
    print("ok  a refused stock is queued for the job, and asking again skips Yahoo")


def test_a_queued_stock_is_tried_live_again_after_the_window():
    from sqlmodel import Session

    from db.models import FundamentalSnapshot

    with temp_store() as engine, yahoo_answers(_refused) as calls:
        fs.get_stock_fundamentals("TCS-EQ")
        with Session(engine) as s:
            row = s.get(FundamentalSnapshot, "TCS")
            row.requested_at = datetime.now(timezone.utc) - store.REQUEST_RETRY_AFTER - timedelta(minutes=1)
            s.add(row)
            s.commit()
        fs.get_stock_fundamentals("TCS-EQ")
        assert calls == ["TCS-EQ", "TCS-EQ"], calls
    print("ok  after the retry window a queued stock is tried live again")


def test_a_live_success_is_stored_for_production():
    with temp_store(), yahoo_answers(lambda s: dict(GOOD, symbol=s)):
        r = fs.get_stock_fundamentals("SUZLON-EQ")
        assert r["error"] is None and "as_of" not in r
    with temp_store(), yahoo_answers(lambda s: dict(GOOD, symbol=s)) as calls:
        fs.get_stock_fundamentals("SUZLON-EQ")
        fs._cache.clear()
        assert fs.get_stock_fundamentals("SUZLON-EQ")["as_of"], "second read comes from the store"
        assert calls == ["SUZLON-EQ"]
    print("ok  a live success is stored, so the next read needs no Yahoo")


def test_without_a_database_the_service_still_fetches_live():
    def broken():
        raise RuntimeError("database down")

    saved, fs_cache = _db_engine_module().get_session, dict(fs._cache)
    _db_engine_module().get_session = broken
    fs._cache.clear()
    try:
        with yahoo_answers(lambda s: dict(GOOD, symbol=s)):
            assert fs.get_stock_fundamentals("SUZLON-EQ")["error"] is None
        fs._cache.clear()
        with yahoo_answers(_refused):
            r = fs.get_stock_fundamentals("TCS-EQ")
            assert r["retryable"] is True and "Couldn't reach Yahoo" in r["error"], r
    finally:
        _db_engine_module().get_session = saved
        fs._cache.clear()
        fs._cache.update(fs_cache)
    print("ok  with the database down, fundamentals still come live from Yahoo")


# ── GitHub's identity token ────────────────────────────────────────────────

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": github_oidc.ISSUER,
        "aud": github_oidc.AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "repository": github_oidc.REPOSITORY,
        "repository_id": github_oidc.REPOSITORY_ID,
        "ref": github_oidc.REF,
        "workflow_ref": f"{github_oidc.REPOSITORY}/{github_oidc.WORKFLOW}@refs/heads/main",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(key=_KEY, **overrides) -> str:
    return jwt.encode(_claims(**overrides), key, algorithm="RS256")


@contextlib.contextmanager
def github_keys():
    """GitHub's key set replaced by our test key's public half."""
    saved = github_oidc._signing_key
    github_oidc._signing_key = lambda token: _KEY.public_key()
    try:
        yield
    finally:
        github_oidc._signing_key = saved


def test_a_token_from_our_workflow_on_main_is_accepted():
    with github_keys():
        assert github_oidc.verify(_token())["repository"] == github_oidc.REPOSITORY
    print("ok  GitHub's token for this repo's workflow on main is accepted")


def test_any_other_token_is_refused():
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = {
        "another repo": _token(repository="someone/fork"),
        "a reused repo name": _token(repository_id="42"),
        "another branch": _token(ref="refs/heads/feature"),
        "a pull request": _token(ref="refs/pull/7/merge"),
        "another workflow": _token(workflow_ref=f"{github_oidc.REPOSITORY}/.github/workflows/x.yml@refs/heads/main"),
        "another audience": _token(aud="sts.amazonaws.com"),
        "another issuer": _token(iss="https://evil.example"),
        "an expired token": _token(iat=int(time.time()) - 3600, exp=int(time.time()) - 600),
        "a forged signature": _token(key=other_key),
        "no repository claim": _token(repository=None),
        "not a token": "not-a-jwt",
    }
    with github_keys():
        for name, token in bad.items():
            try:
                github_oidc.verify(token)
            except github_oidc.NotOurWorkflow:
                continue
            raise AssertionError(f"{name} was accepted")
    print("ok  tokens from other repos, branches, workflows, or forged/expired ones are refused")


def test_the_job_asks_github_for_the_audience_the_app_expects():
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "refresh_fundamentals.py")
    if not os.path.exists(path):
        print("--  scripts/refresh_fundamentals.py not present; skipped")
        return
    spec = importlib.util.spec_from_file_location("refresh_fundamentals", path)
    job = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(job)
    assert job.AUDIENCE == github_oidc.AUDIENCE
    print("ok  the job requests the token audience the app checks")


# ── The endpoints ──────────────────────────────────────────────────────────


@contextlib.contextmanager
def app_client():
    from fastapi.testclient import TestClient

    from web.app import create_app

    with temp_store(), github_keys():
        yield TestClient(create_app(), base_url="https://testserver")


def _auth(token=None) -> dict:
    return {"Authorization": f"Bearer {token or _token()}"}


def test_the_endpoints_refuse_anyone_but_our_workflow():
    with app_client() as client:
        for headers in ({}, {"Authorization": "Basic x"}, _auth(_token(ref="refs/heads/feature"))):
            assert client.get("/api/internal/fundamentals/queue", headers=headers).status_code == 401
            res = client.post("/api/internal/fundamentals", headers=headers, json={"items": [GOOD]})
            assert res.status_code == 401
        assert store.load("SUZLON") is None, "a refused post stores nothing"
    print("ok  without our workflow's token, the queue and the ingest answer 401")


def test_the_job_can_read_the_queue_and_store_results():
    with app_client() as client:
        store.request("TCS-EQ")
        store.request("JUNK-EQ")
        q = client.get("/api/internal/fundamentals/queue", headers=_auth()).json()
        assert sorted(q["wanted"]) == ["JUNK", "TCS"] and q["stored"] == {}, q

        fs._cache["fundamental:TCS-EQ"] = {"data": {"stale": True}, "ts": time.time()}
        res = client.post("/api/internal/fundamentals", headers=_auth(), json={
            "items": [dict(GOOD, symbol="TCS", company_name="Tata Consultancy Services")],
            "no_data": ["JUNK"],
        })
        assert res.status_code == 200 and res.json() == {"saved": 1, "dropped_requests": 1}, res.text
        assert "fundamental:TCS-EQ" not in fs._cache, "a refreshed stock is not served from a stale cache"
        assert fs.get_stock_fundamentals("TCS-EQ")["company_name"] == "Tata Consultancy Services"
        q = client.get("/api/internal/fundamentals/queue", headers=_auth()).json()
        assert q["wanted"] == [] and list(q["stored"]) == ["TCS"], q
    print("ok  the job reads the queue, stores results, and drops no-data requests")


def test_the_ingest_rejects_malformed_or_oversized_posts():
    with app_client() as client:
        for body in ({"items": "x"}, {"items": [], "no_data": "x"}, ["not", "a", "dict"],
                     {"items": [GOOD] * 101}):
            res = client.post("/api/internal/fundamentals", headers=_auth(), json=body)
            assert res.status_code == 400, (body if not isinstance(body, dict) else list(body), res.text)
        res = client.post("/api/internal/fundamentals", headers=_auth(), content=b"{not json")
        assert res.status_code == 400
        res = client.post("/api/internal/fundamentals", json={"items": []},
                          headers={**_auth(), "Content-Length": str(6 * 1024 * 1024)})
        assert res.status_code in (400, 413)
        # Failed and junk entries are ignored, not stored.
        res = client.post("/api/internal/fundamentals", headers=_auth(), json={"items": [
            dict(GOOD, error="No data found"), dict(GOOD, symbol="bad symbol!"), "x",
        ]})
        assert res.json()["saved"] == 0, res.text
    print("ok  malformed, oversized and failed entries are rejected or ignored")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for fn in TESTS:
        fn()
    print(f"\n{len(TESTS)} tests passed")
