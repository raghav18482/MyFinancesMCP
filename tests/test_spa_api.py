"""Tests for the JSON surface the React app uses — login, enrollment, logout,
the session bootstrap, the feedback token, premium registration — and for
serving the built app itself.

The broker is replaced by a fake client and the database by a temporary sqlite
file, so these run offline. The server-rendered login and enroll forms are
exercised too: they now share :mod:`web.auth_flow` with the JSON endpoints, and
the refactor must not have changed what they do.

Run directly (``python tests/test_spa_api.py``) or under pytest.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

GOOD_PASSWORD = "correct-horse-battery"
WRONG_PASSWORD = "incorrect-horse-batter"
NUMBER = "+919876543210"

ENROLL = {
    "api_key": "APIKEY123",
    "client_id": "AB1234",
    "password": "4321",
    "totp_secret": "JBSWY3DPEHPK3PXP",
    "country_code": "91",
    "whatsapp_number": "98765 43210",
    "app_password": GOOD_PASSWORD,
    "app_password_confirm": GOOD_PASSWORD,
}


class FakeAngelClient:
    """Stands in for ``AngelOneClient``: keeps its credentials, calls nobody.

    The broker payloads are class attributes so a test can swap one out, or set
    it to an exception to simulate the broker failing.
    """

    positions: object = {"status": True, "data": [
        {"tradingsymbol": "INFY-EQ", "producttype": "INTRADAY", "netqty": "10",
         "buyavgprice": "1500.5", "sellavgprice": "0", "ltp": "1510", "pnl": "95.0"},
        {"tradingsymbol": "TCS-EQ", "producttype": "DELIVERY", "netqty": "-2",
         "buyavgprice": "0", "sellavgprice": "3900", "ltp": "3910.25", "pnl": "-20.5"},
    ]}
    orders: object = {"status": True, "data": [
        {"orderid": "O1", "tradingsymbol": "INFY-EQ", "transactiontype": "BUY",
         "quantity": "10", "price": "1500.5", "status": "complete", "updatetime": "09:20"},
    ]}
    trades: object = {"status": True, "data": [
        {"tradeid": "T1", "orderid": "O1", "tradingsymbol": "INFY-EQ", "transactiontype": "BUY",
         "fillsize": "10", "fillprice": "1500.5", "filltime": "09:20:01",
         "exchange": "NSE", "producttype": "INTRADAY"},
        # An older payload shape with no fill fields: falls back to the order's own.
        {"tradeid": "T2", "orderid": "O2", "tradingsymbol": "TCS-EQ", "transactiontype": "SELL",
         "quantity": "2", "price": "3900", "updatetime": "10:01", "exchange": "NSE",
         "producttype": "DELIVERY"},
    ]}

    def __init__(self, api_key, client_id, password, totp_secret):
        self.api_key = api_key
        self.client_id = client_id
        self.password = password
        self.totp_secret = totp_secret

    def _answer(self, payload):
        if isinstance(payload, Exception):
            raise payload
        return payload

    def get_positions(self):
        return self._answer(self.positions)

    def get_order_book(self):
        return self._answer(self.orders)

    def get_trade_book(self):
        return self._answer(self.trades)


class Harness:
    def __init__(self, client, service, live, broker):
        self.http = client
        self.service = service
        self.live = live  # sid -> FakeAngelClient, the in-memory broker sessions
        self.broker = broker  # {"accepts": bool}

    def bootstrap(self) -> dict:
        res = self.http.get("/api/session/bootstrap")
        assert res.status_code == 200, res.text
        return res.json()

    def enrol_row(self, **overrides) -> dict:
        return self.service.create_user(**{
            "whatsapp_number": NUMBER,
            "angel_api_key": "APIKEY123",
            "angel_client_id": "AB1234",
            "angel_password": "4321",
            "angel_totp_secret": "JBSWY3DPEHPK3PXP",
            "app_password": GOOD_PASSWORD,
            **overrides,
        })


@contextlib.contextmanager
def harness():
    """The real app over a temp database and a broker that says yes by default."""
    from sqlmodel import Session, SQLModel, create_engine

    import web.dependencies as deps
    import web.routers.briefing as briefing_router
    from adminApi import service
    from db import models  # noqa: F401 — registers the tables
    from services import login_throttle
    from session_manager import sessions
    from web.app import create_app

    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
    tmp = tempfile.TemporaryDirectory()
    engine = create_engine(f"sqlite:///{os.path.join(tmp.name, 't.db')}")
    SQLModel.metadata.create_all(engine)

    def factory():
        return Session(engine)

    live: dict[str, FakeAngelClient] = {}
    broker = {"accepts": True}

    def create_session(sid, api_key, client_id, password, totp_secret):
        if not broker["accepts"]:
            raise RuntimeError("Invalid totp")
        live[sid] = FakeAngelClient(api_key, client_id, password, totp_secret)
        return live[sid]

    undo = []

    def patch(obj, name, value):
        if name in vars(obj):
            original = vars(obj)[name]
            undo.append(lambda: setattr(obj, name, original))
        else:  # a method looked up through the class: drop the shadow afterwards
            undo.append(lambda: delattr(obj, name))
        setattr(obj, name, value)

    # ``from db import get_session`` copies the name into each module, so it has
    # to be replaced where it is used, not where it is defined.
    patch(service, "get_session", factory)
    patch(deps, "get_session", factory)
    patch(briefing_router, "get_session", factory)
    patch(sessions, "create_session", create_session)
    patch(sessions, "get_client", live.get)
    patch(sessions, "remove_session", lambda sid: live.pop(sid, None))
    login_throttle._by_identifier.clear()
    login_throttle._by_ip.clear()

    try:
        # https, so the session cookie is sent even if SESSION_COOKIE_SECURE=1
        # came in from a local .env.
        client = TestClient(create_app(), base_url="https://testserver")
        yield Harness(client, service, live, broker)
    finally:
        for fn in reversed(undo):
            fn()
        tmp.cleanup()


# ── Config and bootstrap ───────────────────────────────────────────────────


def test_config_carries_what_the_forms_need():
    from db.passwords import MIN_PASSWORD_CHARS

    with harness() as h:
        data = h.http.get("/api/auth/config").json()

    assert data["default_dial_code"] == "91"
    assert data["countries"][0]["dial"] == "91", "India must lead the dropdown"
    assert {"iso", "dial", "name", "label"} <= set(data["countries"][0])
    assert data["min_password_chars"] == MIN_PASSWORD_CHARS
    print("ok  /api/auth/config carries countries, default code and password rule")


def test_bootstrap_is_a_200_for_anonymous_visitors():
    with harness() as h:
        res = h.http.get("/api/session/bootstrap")

    assert res.status_code == 200, "anonymous is an answer, not an error"
    assert res.json() == {"authenticated": False}
    assert res.headers["cache-control"] == "no-store"
    print("ok  bootstrap answers 200 {authenticated: false} and is never cached")


# ── Enrollment ─────────────────────────────────────────────────────────────


def test_enroll_signs_in_and_bootstrap_reports_the_user():
    with harness() as h:
        res = h.http.post("/api/auth/enroll", json=ENROLL)
        assert res.status_code == 200, res.text
        assert res.json() == {"ok": True, "client_id": "AB1234"}

        me = h.bootstrap()

    assert me["authenticated"] is True
    assert me["client_id"] == "AB1234"
    assert me["premium"] == {"registered": True, "whatsapp_number": NUMBER}
    print("ok  enrolling signs the browser in, and bootstrap reports who it is")


def test_enroll_rejections_name_the_field_at_fault():
    cases = [
        ({"app_password_confirm": GOOD_PASSWORD + "x"}, "app_password_confirm"),
        ({"app_password": "short", "app_password_confirm": "short"}, "app_password"),
        ({"country_code": "999"}, "whatsapp_number"),
        ({"whatsapp_number": "12"}, "whatsapp_number"),
    ]
    with harness() as h:
        for overrides, field in cases:
            res = h.http.post("/api/auth/enroll", json={**ENROLL, **overrides})
            assert res.status_code == 400, (overrides, res.text)
            assert res.json()["field"] == field, (overrides, res.json())
        assert h.bootstrap() == {"authenticated": False}
        assert not h.live, "a rejected form must not leave a broker session behind"
    print("ok  each enrollment rejection names the input to fix")


def test_enroll_reports_broker_rejection_and_stores_nothing():
    with harness() as h:
        h.broker["accepts"] = False
        res = h.http.post("/api/auth/enroll", json=ENROLL)

        assert res.status_code == 400
        assert res.json()["error"].startswith("Angel One rejected these credentials")
        assert "field" not in res.json()
        assert h.service.find_by_client_id("AB1234") is None
        assert h.bootstrap() == {"authenticated": False}
    print("ok  a broker rejection is reported and nothing is stored")


def test_enroll_refuses_a_number_owned_by_another_account():
    with harness() as h:
        h.enrol_row(angel_client_id="ZZ9999")
        res = h.http.post("/api/auth/enroll", json=ENROLL)

        assert res.status_code == 409, res.text
        assert res.json()["field"] == "whatsapp_number"
        assert not h.live, "the verified-but-unsaved broker session must be dropped"
        assert h.bootstrap() == {"authenticated": False}
    print("ok  a number already owned elsewhere is refused and the session dropped")


# ── Login and logout ───────────────────────────────────────────────────────


def test_login_then_logout_round_trip():
    with harness() as h:
        h.enrol_row()
        res = h.http.post("/api/auth/login", json={
            "country_code": "+91",
            "whatsapp_number": "098765-43210",  # normalises to NUMBER
            "app_password": GOOD_PASSWORD,
        })
        assert res.status_code == 200, res.text
        assert res.json() == {"ok": True, "client_id": "AB1234"}
        assert h.bootstrap()["authenticated"] is True

        res = h.http.post("/api/auth/logout")
        assert res.json() == {"ok": True}
        assert h.bootstrap() == {"authenticated": False}
        assert not h.live, "logout must end the broker session"
    print("ok  JSON login signs in, JSON logout signs out and ends the broker session")


def test_login_misses_share_one_vague_message():
    """Unknown number and wrong password must be indistinguishable, or the
    endpoint becomes an oracle for which numbers are registered."""
    with harness() as h:
        h.enrol_row()
        unknown = h.http.post("/api/auth/login", json={
            "country_code": "91", "whatsapp_number": "9000000000",
            "app_password": GOOD_PASSWORD,
        })
        wrong = h.http.post("/api/auth/login", json={
            "country_code": "91", "whatsapp_number": "9876543210",
            "app_password": WRONG_PASSWORD,
        })

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert "field" not in wrong.json(), "naming a field would leak which part was wrong"
    print("ok  an unknown number and a wrong password get the identical 401")


def test_login_is_throttled_with_retry_after():
    from services import login_throttle

    with harness() as h:
        h.enrol_row()
        bad = {"country_code": "91", "whatsapp_number": "9876543210",
               "app_password": WRONG_PASSWORD}
        for _ in range(login_throttle._MAX_PER_IDENTIFIER):
            assert h.http.post("/api/auth/login", json=bad).status_code == 401

        res = h.http.post("/api/auth/login", json=bad)

    assert res.status_code == 429, res.text
    assert int(res.headers["retry-after"]) > 0
    assert res.json()["error"].startswith("Too many login attempts")
    print("ok  repeated failures are throttled with a 429 and Retry-After")


def test_login_reports_a_broker_rejection_separately():
    with harness() as h:
        h.enrol_row()
        h.broker["accepts"] = False
        res = h.http.post("/api/auth/login", json={
            "country_code": "91", "whatsapp_number": "9876543210",
            "app_password": GOOD_PASSWORD,
        })

    assert res.status_code == 502, res.text
    assert "Angel One rejected the stored credentials" in res.json()["error"]
    print("ok  a right password with stale broker credentials is a distinct 502")


def test_malformed_bodies_are_refused():
    with harness() as h:
        for path in ("/api/auth/login", "/api/auth/enroll"):
            not_json = h.http.post(path, content=b"phone=1", headers={
                "content-type": "application/x-www-form-urlencoded"})
            a_list = h.http.post(path, json=["not", "an", "object"])
            assert not_json.status_code == a_list.status_code == 400, path
            assert not_json.json() == {"error": "Invalid JSON body"}
    print("ok  a non-JSON or non-object body is a 400, not a 500")


# ── The server-rendered forms still work through the shared flow ───────────


def test_form_login_still_redirects_and_re_renders_on_failure():
    with harness() as h:
        h.enrol_row()
        form = {"country_code": "91", "whatsapp_number": "9876543210"}

        miss = h.http.post("/login", data={**form, "app_password": WRONG_PASSWORD},
                           follow_redirects=False)
        assert miss.status_code == 200
        assert "don&#39;t match" in miss.text or "don't match" in miss.text

        hit = h.http.post("/login", data={**form, "app_password": GOOD_PASSWORD},
                          follow_redirects=False)
        assert hit.status_code == 302 and hit.headers["location"] == "/dashboard"

        out = h.http.post("/logout", follow_redirects=False)
        assert out.status_code == 302 and out.headers["location"] == "/"
        assert h.bootstrap() == {"authenticated": False}
    print("ok  the Jinja login and logout behave exactly as before the refactor")


def test_form_enroll_still_redirects_and_re_renders_on_failure():
    with harness() as h:
        mismatch = h.http.post("/enroll", data={**ENROLL, "app_password_confirm": "nope"},
                               follow_redirects=False)
        assert mismatch.status_code == 200
        assert "The two passwords do not match." in mismatch.text
        assert "APIKEY123" in mismatch.text, "non-secret fields should be echoed back"

        ok = h.http.post("/enroll", data=ENROLL, follow_redirects=False)
        assert ok.status_code == 302 and ok.headers["location"] == "/dashboard"
        assert h.bootstrap()["client_id"] == "AB1234"
    print("ok  the Jinja enroll form behaves exactly as before the refactor")


# ── Feedback token and premium registration ────────────────────────────────


def test_feedback_token_is_single_use():
    from services import feedback as feedback_service

    with harness() as h:
        res = h.http.get("/api/feedback/token")

    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    token = res.json()["token"]
    feedback_service.consume_feedback_token(token)
    try:
        feedback_service.consume_feedback_token(token)
    except feedback_service.FeedbackError as e:
        assert e.status_code == 403
    else:
        raise AssertionError("a feedback token was accepted twice")
    print("ok  the minted feedback token is accepted once and only once")


def test_premium_register_normalises_a_dropdown_number():
    with harness() as h:
        h.http.post("/api/auth/enroll", json=ENROLL)
        row = h.service.find_by_client_id("AB1234")
        h.service.deactivate_user(row["id"])

        bad = h.http.post("/api/premium/register",
                          json={"country_code": "999", "whatsapp_number": "9876543210"})
        assert bad.status_code == 400

        res = h.http.post("/api/premium/register",
                          json={"country_code": "91", "whatsapp_number": "09876 543210"})
        assert res.status_code == 200, res.text
        stored = h.service.find_by_client_id("AB1234")

    assert stored["whatsapp_number"] == NUMBER
    assert stored["is_active"] is True
    print("ok  premium registration stores the same E.164 number login looks up")


# ── Positions, orders and trades ───────────────────────────────────────────


@contextlib.contextmanager
def signed_in():
    with harness() as h:
        h.http.post("/api/auth/enroll", json=ENROLL)
        yield h


@contextlib.contextmanager
def broker_payload(**payloads):
    """Temporarily replace FakeAngelClient's canned broker answers."""
    saved = {k: getattr(FakeAngelClient, k) for k in payloads}
    for k, v in payloads.items():
        setattr(FakeAngelClient, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(FakeAngelClient, k, v)


def test_positions_are_mapped_and_totalled():
    with signed_in() as h:
        data = h.http.get("/api/portfolio/positions").json()

    assert data["total_pnl"] == 74.5
    assert data["positions"][0] == {
        "symbol": "INFY-EQ", "product": "INTRADAY", "net_qty": 10, "buy_avg": 1500.5,
        "sell_avg": 0.0, "ltp": 1510.0, "pnl": 95.0,
    }
    assert data["positions"][1]["net_qty"] == -2
    print("ok  positions come back as typed rows with their day P&L total")


def test_orders_and_trades_are_separate_and_trades_fall_back():
    with signed_in() as h:
        orders = h.http.get("/api/portfolio/orders").json()["orders"]
        trades = h.http.get("/api/portfolio/trades").json()["trades"]

    assert orders == [{"orderid": "O1", "symbol": "INFY-EQ", "txn_type": "BUY", "qty": 10,
                       "price": 1500.5, "status": "complete", "time": "09:20"}]
    assert trades[0]["qty"] == 10 and trades[0]["time"] == "09:20:01"
    assert (trades[1]["qty"], trades[1]["price"], trades[1]["time"]) == (2, 3900.0, "10:01"), (
        "a fill-less trade must fall back to the order's quantity, price and time"
    )
    print("ok  order book and trade book are separate, and old trade shapes still map")


def test_empty_broker_answers_are_empty_lists():
    nothing = broker_payload(positions={"status": True, "data": None},
                             orders={"status": False, "message": "no orders"})
    with nothing, signed_in() as h:
        assert h.http.get("/api/portfolio/positions").json() == {"positions": [], "total_pnl": 0}
        assert h.http.get("/api/portfolio/orders").json() == {"orders": []}
    print("ok  'nothing today' from the broker is an empty list, not an error")


def test_a_broker_failure_is_a_502_and_does_not_hide_the_other_book():
    with broker_payload(trades=RuntimeError("trade book down")), signed_in() as h:
        trades = h.http.get("/api/portfolio/trades")
        orders = h.http.get("/api/portfolio/orders")

    assert trades.status_code == 502 and trades.json() == {"error": "trade book down"}
    assert orders.status_code == 200 and len(orders.json()["orders"]) == 1
    print("ok  a trade-book outage is a 502 that leaves the order book working")


def test_broker_endpoints_require_a_session():
    with harness() as h:
        for path in ("/api/portfolio/positions", "/api/portfolio/orders", "/api/portfolio/trades"):
            assert h.http.get(path).status_code == 401, path
    print("ok  positions, orders and trades all answer 401 when signed out")


def test_jinja_pages_render_the_same_rows():
    with signed_in() as h:
        positions = h.http.get("/positions").text
        orders = h.http.get("/orders").text

    assert "INFY-EQ" in positions and "TCS-EQ" in positions and "74.50" in positions
    assert "T2" in orders and "3900.00" in orders, "the trade fallback must reach the template"
    print("ok  the server-rendered pages still show the same rows via the shared mapping")


# ── Briefing schedule ──────────────────────────────────────────────────────


def test_briefing_schedule_saves_with_an_aware_next_run():
    """Regression: the router used to store a naive datetime, which the
    ``schedules.next_run`` column rejects, so every save was a 500."""
    from datetime import datetime, timedelta, timezone

    with signed_in() as h:
        res = h.http.post("/api/briefing/schedule", json={"time_ist": "08:30", "enabled": True})
        assert res.status_code == 200, res.text
        saved = res.json()["schedule"]

        again = h.http.get("/api/briefing/schedule").json()
        assert again["exists"] and again["schedule"]["id"] == saved["id"]

    next_run = datetime.fromisoformat(saved["next_run"])
    assert next_run.tzinfo is not None, "next_run must be timezone-aware"
    ist = next_run.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert (ist.hour, ist.minute) == (8, 30), ist
    assert next_run > datetime.now(timezone.utc), "the next run must be in the future"
    print("ok  a briefing schedule saves, and next_run is aware UTC at 08:30 IST")


# ── Research summary uses the deployment's key ─────────────────────────────


@contextlib.contextmanager
def summary_stubs(env_key):
    """Stub the Yahoo and OpenRouter calls, recording what the LLM was handed."""
    import web.routers.research as research

    calls: list[dict] = []

    async def fake_summarize(api_key, fundamentals, model):
        calls.append({"api_key": api_key, "model": model})
        return f"summary #{len(calls)}"

    saved = (research.get_stock_fundamentals, research.summarize_fundamentals, dict(research._summary_cache))
    research.get_stock_fundamentals = lambda symbol: {"symbol": symbol, "valuation": {}, "health": {}}
    research.summarize_fundamentals = fake_summarize
    research._summary_cache.clear()
    old_env = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "RESEARCH_SUMMARY_MODEL")}
    os.environ.pop("RESEARCH_SUMMARY_MODEL", None)
    if env_key is None:
        os.environ.pop("OPENROUTER_API_KEY", None)
    else:
        os.environ["OPENROUTER_API_KEY"] = env_key
    try:
        yield calls, research
    finally:
        research.get_stock_fundamentals, research.summarize_fundamentals = saved[0], saved[1]
        research._summary_cache.clear()
        research._summary_cache.update(saved[2])
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_research_summary_uses_the_server_key_and_ignores_the_callers():
    with summary_stubs("server-key") as (calls, _), signed_in() as h:
        res = h.http.post("/api/research/fundamental/summary", json={
            "symbol": "TCS-EQ", "api_key": "caller-key", "model": "expensive/model",
        })
        assert res.status_code == 200, res.text
        assert res.json() == {"summary": "summary #1", "cached": False}
        assert calls == [{"api_key": "server-key", "model": "openai/gpt-4o-mini"}], calls
    print("ok  the summary is generated with the server's key and model, not the caller's")


def test_research_summary_needs_no_key_from_the_user_and_caches():
    with summary_stubs("server-key") as (calls, _), signed_in() as h:
        first = h.http.post("/api/research/fundamental/summary", json={"symbol": "TCS-EQ"})
        second = h.http.post("/api/research/fundamental/summary", json={"symbol": "TCS-EQ"})
        assert first.status_code == 200 and second.json()["cached"] is True
        assert len(calls) == 1
    print("ok  no caller key needed; the second request is a cache hit")


def test_research_summary_refresh_is_rate_limited():
    with summary_stubs("server-key") as (calls, research), signed_in() as h:
        body = {"symbol": "TCS-EQ", "refresh": True}
        h.http.post("/api/research/fundamental/summary", json=body)
        again = h.http.post("/api/research/fundamental/summary", json=body)
        assert again.json()["cached"] is True and len(calls) == 1, "a refresh inside the cooldown must not spend credit"
        # Age the entry past the cooldown: now a refresh really regenerates.
        for entry in research._summary_cache.values():
            entry["ts"] -= research._SUMMARY_REFRESH_COOLDOWN + 1
        later = h.http.post("/api/research/fundamental/summary", json=body)
        assert later.json() == {"summary": "summary #2", "cached": False} and len(calls) == 2
    print("ok  Regenerate skips the cache, but not more than once a minute")


def test_research_summary_is_a_clean_503_when_the_server_has_no_key():
    with summary_stubs(None) as (calls, _), signed_in() as h:
        res = h.http.post("/api/research/fundamental/summary", json={"symbol": "TCS-EQ", "api_key": "caller-key"})
        assert res.status_code == 503 and "error" in res.json()
        assert not calls, "a caller's key must not be a fallback"
    print("ok  without OPENROUTER_API_KEY the endpoint answers 503 and calls nothing")


# ── Serving the React build ────────────────────────────────────────────────


@contextlib.contextmanager
def built_app(files: dict[str, str] | None):
    """``create_app()`` with ``SPA_DIST_DIR`` pointed at a fake build, or unset."""
    from web.app import create_app

    tmp = tempfile.TemporaryDirectory()
    for rel, body in (files or {}).items():
        path = os.path.join(tmp.name, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)

    previous = os.environ.get("SPA_DIST_DIR")
    if files is None:
        os.environ.pop("SPA_DIST_DIR", None)
    else:
        os.environ["SPA_DIST_DIR"] = tmp.name
    try:
        yield TestClient(create_app())
    finally:
        if previous is None:
            os.environ.pop("SPA_DIST_DIR", None)
        else:
            os.environ["SPA_DIST_DIR"] = previous
        tmp.cleanup()


SHELL = '<!doctype html><div id="root"></div>'


def test_spa_answers_deep_links_with_the_shell():
    with built_app({"index.html": SHELL, "assets/index-abc123.js": "console.log(1)"}) as c:
        for path in ("/app/", "/app/dashboard", "/app/research/fundamental"):
            res = c.get(path)
            assert res.status_code == 200, path
            assert res.text == SHELL, path
            assert res.headers["cache-control"] == "no-cache", path

        asset = c.get("/app/assets/index-abc123.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"

        assert c.get("/app/assets/index-gone999.js").status_code == 404, (
            "a missing asset must 404, not be answered with HTML"
        )
        assert c.get("/api/session/bootstrap").json() == {"authenticated": False}, (
            "the SPA must never shadow an API route"
        )
    print("ok  deep links get the shell, assets cache forever, misses 404, APIs win")


def test_spa_is_not_mounted_without_a_usable_build():
    for files in (None, {"not-index.txt": "x"}):
        with built_app(files) as c:
            mounts = {r.path for r in c.app.routes if type(r).__name__ == "Mount"}
            assert "/app" not in mounts, files
            assert c.get("/app/dashboard").status_code == 404
    print("ok  no SPA_DIST_DIR, or one without index.html, mounts nothing")


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
    print(f"\n{passed} tests passed")
