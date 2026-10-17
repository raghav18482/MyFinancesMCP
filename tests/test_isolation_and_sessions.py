"""Cross-user isolation and web session lifecycle.

  * Every user-owned resource — decisions, chat threads, briefing schedules and
    logs, portfolio data — is visible only to its owner. Decisions with unknown
    ownership are shown to nobody.
  * Logout revokes every saved copy of the stateless cookie, on every device,
    and releases the session's runners and caches.
  * An expired broker session is rebuilt once per user however many requests
    race, and only for a cookie whose session version still matches.
  * Forged, tampered or wrongly-signed cookies get nothing.
  * Production refuses to start without its security settings.

Fake broker and temporary SQLite only. Run directly or under pytest.
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _security_harness import PASSWORD, USERS, FakeBroker, harness, run_all  # noqa: E402


def _live_sids():
    import session_manager

    return [k for k in session_manager.sessions._sessions if not k.startswith("mcp-user-")]


def test_decisions_are_owner_only_and_unknown_owners_are_hidden():
    from services import decision_record

    with harness() as h:
        db_engine = sys.modules["db.engine"]
        assert db_engine.get_session is not None
        for uid, sym in ((h.ids["A"], "INFY-EQ"), (h.ids["B"], "TCS-EQ"), (None, "LEGACY-EQ")):
            assert decision_record.record(proposal_id=f"p-{sym}", symbol=sym, side="BUY",
                                          quantity=1, outcome="executed", user_id=uid,
                                          session_ref=f"sess-{sym}") is not None

        a, b = h.login("A"), h.login("B")
        rows_a = a.get("/api/trading/decisions").json()["decisions"]
        rows_b = b.get("/api/trading/decisions?limit=200").json()["decisions"]
        assert [r["symbol"] for r in rows_a] == ["INFY-EQ"], rows_a
        assert [r["symbol"] for r in rows_b] == ["TCS-EQ"], rows_b
        assert a.get("/api/trading/decisions?symbol=TCS-EQ").json()["decisions"] == []
        assert a.get("/api/trading/decisions?symbol=LEGACY-EQ").json()["decisions"] == []

        assert decision_record.recent(200, user_id=None) == []
        assert h.client().get("/api/trading/decisions").status_code == 401
    print("ok  decisions: each user sees only their own; NULL-owner rows are shown to nobody")


def test_chat_threads_are_private():
    import web.routers.agent as agent_router

    with harness() as h:
        a, b = h.login("A"), h.login("B")
        made = a.post("/api/agent/threads", json={"agent_type": "finance"})
        assert made.status_code == 200, made.text
        tid = made.json()["thread"]["id"]

        async def fake_messages(**kw):
            return [{"role": "user", "text": f"secret question for {kw['user_id']}"}]

        async def fake_chat(**kw):  # pragma: no cover — must not be reached for B
            raise AssertionError("B's chat reached the agent with A's thread")

        orig_msgs, orig_chat = agent_router.registry.get_messages, agent_router.registry.chat
        agent_router.registry.get_messages, agent_router.registry.chat = fake_messages, fake_chat
        try:
            assert a.get(f"/api/agent/threads/{tid}/messages").status_code == 200
            assert b.get(f"/api/agent/threads/{tid}/messages").status_code == 404
            assert b.patch(f"/api/agent/threads/{tid}", json={"title": "pwned"}).status_code == 404
            b.delete(f"/api/agent/threads/{tid}")
            assert b.post("/api/agent/chat", json={"message": "hi", "thread_id": tid}).status_code == 404
            assert tid not in [t["id"] for t in b.get("/api/agent/threads").json()["threads"]]
        finally:
            agent_router.registry.get_messages, agent_router.registry.chat = orig_msgs, orig_chat

        threads_a = a.get("/api/agent/threads").json()["threads"]
        assert [t["id"] for t in threads_a] == [tid]
        assert threads_a[0]["title"] == "New conversation", "B renamed A's thread"
    print("ok  B cannot read, rename, delete or chat in A's conversation")


def test_briefing_schedules_and_logs_are_private():
    from datetime import timedelta

    from db.models import Log, Schedule, utcnow

    with harness() as h:
        with h.db() as s:
            sched = Schedule(user_id=h.ids["A"], interval_minutes=1440,
                             next_run=utcnow() + timedelta(days=1))
            s.add(sched)
            s.add(Log(user_id=h.ids["A"], status="ok", message="A's private briefing"))
            s.add(Log(user_id=h.ids["B"], status="ok", message="B's private briefing"))
            s.commit()

        a, b = h.login("A"), h.login("B")
        logs_a = [l["message"] for l in a.get("/api/briefing/logs").json()["logs"]]
        logs_b = [l["message"] for l in b.get("/api/briefing/logs").json()["logs"]]
        assert logs_a == ["A's private briefing"] and logs_b == ["B's private briefing"]
        assert a.get("/api/briefing/schedule").json()["exists"] is True
        assert b.get("/api/briefing/schedule").json()["exists"] is False
    print("ok  briefing schedules and logs are per user")


def test_holdings_positions_and_analytics_are_per_user():
    import services.portfolio_service as portfolio_service

    with harness() as h:
        original = portfolio_service.build_portfolio_data
        portfolio_service.build_portfolio_data = lambda client: {"owner": client.client_id}
        try:
            a, b = h.login("A"), h.login("B")
            assert a.get("/api/portfolio/analytics").json() == {"owner": "AA1111"}
            assert b.get("/api/portfolio/analytics").json() == {"owner": "BB2222"}
            pa = a.get("/api/portfolio/positions").json()["positions"]
            pb = b.get("/api/portfolio/positions").json()["positions"]
            assert [p["symbol"] for p in pa] == ["INFY-EQ"] and [p["symbol"] for p in pb] == ["TCS-EQ"]
        finally:
            portfolio_service.build_portfolio_data = original
    print("ok  portfolio analytics and positions come from each user's own broker session")


def _cookie(http):
    return http.cookies.get("session")


def test_a_cookie_saved_before_logout_is_refused_after_it():
    from db.models import User

    with harness() as h:
        a = h.login("A")
        saved = _cookie(a)
        assert a.get("/api/portfolio/positions").status_code == 200
        with h.db() as s:
            before = s.get(User, h.ids["A"]).session_version

        assert a.post("/api/auth/logout").status_code == 200
        assert a.get("/api/portfolio/positions").status_code == 401

        replay = h.client()
        replay.cookies.set("session", saved)
        assert replay.get("/api/portfolio/positions").status_code == 401
        assert replay.get("/api/session/bootstrap").json() == {"authenticated": False}
        with h.db() as s:
            assert s.get(User, h.ids["A"]).session_version == before + 1
        assert not _live_sids(), "logout left a broker session alive"
    print("ok  a cookie copied before logout is refused afterwards (session version bumped)")


def test_logout_on_one_device_ends_the_others():
    with harness() as h:
        d1, d2 = h.login("A"), h.login("A")
        assert d2.get("/api/portfolio/positions").status_code == 200
        d1.post("/api/auth/logout")
        assert d2.get("/api/portfolio/positions").status_code == 401
        b = h.login("B")
        assert b.get("/api/portfolio/positions").status_code == 200, "A's logout affected B"
    print("ok  logging out on one device revokes A's other devices, and nobody else")


def test_expired_broker_session_rebuilds_only_for_a_valid_cookie():
    import session_manager

    with harness() as h:
        a = h.login("A")
        logins = FakeBroker.logins["AA1111"]
        for sid in _live_sids():
            session_manager.sessions.remove_session(sid)  # 8h TTL elapsed / restart
        assert a.get("/api/portfolio/positions").status_code == 200
        assert FakeBroker.logins["AA1111"] == logins + 1

        h.service.deactivate_user(h.ids["A"])
        assert a.get("/api/portfolio/positions").status_code == 401
        assert not _live_sids(), "a deactivated user's live session survived"
    print("ok  an expired session is rebuilt for a valid cookie; deactivation ends it at once")


def test_forged_tampered_and_foreign_cookies_get_nothing():
    import base64
    import json

    import itsdangerous

    with harness() as h:
        a = h.login("A")
        good = _cookie(a)

        for value in ("garbage", good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB"),
                      good.replace(".", "x", 1)):
            c = h.client()
            c.cookies.set("session", value)
            assert c.get("/api/portfolio/positions").status_code == 401, value[:12]

        payload = base64.b64encode(json.dumps({"uid": h.ids["B"], "uv": 0,
                                               "sid": "x" * 32}).encode())
        forged = itsdangerous.TimestampSigner("not-the-server-secret").sign(payload).decode()
        c = h.client()
        c.cookies.set("session", forged)
        assert c.get("/api/portfolio/positions").status_code == 401
        assert c.get("/api/trading/decisions").status_code == 401
    print("ok  garbage, tampered and wrongly-signed cookies are all refused")


def test_concurrent_rebuilds_log_in_once():
    import session_manager

    with harness() as h:
        a = h.login("A")
        saved = _cookie(a)
        for sid in _live_sids():
            session_manager.sessions.remove_session(sid)
        logins = FakeBroker.logins["AA1111"]
        FakeBroker.login_delay = 0.2
        codes = []

        def worker():
            c = h.client()
            c.cookies.set("session", saved)
            codes.append(c.get("/api/portfolio/positions").status_code)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        FakeBroker.login_delay = 0.0
        assert codes == [200] * 8, codes
        assert FakeBroker.logins["AA1111"] == logins + 1, FakeBroker.logins
        assert len(_live_sids()) == 1
    print("ok  eight concurrent requests with an expired session share one broker login")


def test_logout_releases_runners_and_caches():
    import services.portfolio_service as portfolio_service
    from services.adk_runner_registry import registry
    from services.risk_profile import build_profile_from_dict, risk_profiles

    with harness() as h:
        a = h.login("A")
        (sid,) = _live_sids()
        registry._runners[(sid, "finance", False)] = object()
        portfolio_service._portfolio_cache[sid] = (0.0, {"owner": "AA1111"})
        risk_profiles.set(sid, build_profile_from_dict({
            "age": 30, "goal": "income", "horizon_years": 5, "risk_tolerance": "medium",
            "tax_bracket": "30", "max_single_order_value": 10000, "max_position_pct": 10,
            "allowed_products": ["DELIVERY"], "max_daily_trades": 3,
        }))

        a.post("/api/auth/logout")
        assert not [k for k in registry._runners if k[0] == sid]
        assert sid not in portfolio_service._portfolio_cache
        assert risk_profiles.get(sid) is None
    print("ok  logout evicts the session's ADK runners, portfolio cache and risk-profile cache")


def test_shared_caches_hold_no_user_data():
    import services.market_data as market_data
    import web.routers.research as research

    with harness() as h:
        a, b = h.login("A"), h.login("B")
        for c in (a, b):
            c.get("/api/portfolio/candles?symbol=RELIANCE")
        blob = repr(market_data._search_scrip_cache) + repr(research._summary_cache)
        for u in USERS.values():
            for v in (u["client_id"], u["symbol"], u["e164"]):
                assert v not in blob, f"{v} found in a shared cache"
        for key in market_data._search_scrip_cache:
            assert len(key) == 2, key  # (exchange, search key): public instrument data
    print("ok  the process-wide caches hold only public instrument data")


def test_production_refuses_insecure_configuration():
    from web import security_config
    from web.app import create_app

    keys = ("APP_ENV", "SESSION_SECRET", "ALLOWED_HOSTS", "PUBLIC_BASE_URL", "ENCRYPTION_KEY",
            "SESSION_COOKIE_SECURE", "RENDER")
    saved = {k: os.environ.get(k) for k in keys}

    def setenv(**kv):
        for k in keys:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if k == "ENCRYPTION_KEY" and v:
                os.environ[k] = v
        os.environ.update({k: v for k, v in kv.items() if v is not None})

    try:
        for missing in ("SESSION_SECRET", "ALLOWED_HOSTS", "PUBLIC_BASE_URL"):
            env = {"APP_ENV": "production", "SESSION_SECRET": "s" * 40,
                   "ALLOWED_HOSTS": "example.com", "PUBLIC_BASE_URL": "https://example.com"}
            env.pop(missing)
            setenv(**env)
            try:
                create_app()
            except security_config.InsecureConfiguration:
                pass
            else:
                raise AssertionError(f"production started without {missing}")

        setenv(APP_ENV="production", SESSION_SECRET="short", ALLOWED_HOSTS="example.com",
               PUBLIC_BASE_URL="https://example.com")
        try:
            create_app()
        except security_config.InsecureConfiguration:
            pass
        else:
            raise AssertionError("production started with a short SESSION_SECRET")

        setenv(APP_ENV="production", SESSION_SECRET="s" * 40, ALLOWED_HOSTS="testserver",
               PUBLIC_BASE_URL="https://example.com", SESSION_COOKIE_SECURE="0")
        try:
            create_app()
        except security_config.InsecureConfiguration:
            pass
        else:
            raise AssertionError("production started with SESSION_COOKIE_SECURE=0")

        setenv(APP_ENV="production", SESSION_SECRET="s" * 40, ALLOWED_HOSTS="testserver",
               PUBLIC_BASE_URL="https://example.com")
        with harness() as h:
            for path in ("/docs", "/redoc", "/openapi.json"):
                assert h.client().get(path).status_code == 404, path
            c = h.client()
            res = c.post("/api/auth/login", json={"country_code": "91",
                                                  "whatsapp_number": USERS["A"]["national"],
                                                  "app_password": PASSWORD})
            assert res.status_code == 200, res.text
            assert "secure" in res.headers.get("set-cookie", "").lower()
            evil = h.client()
            assert evil.get("/", headers={"host": "attacker.example"}).status_code == 400
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok  production refuses missing/weak secrets and hosts; docs off; cookie Secure")


def test_admin_key_check_is_constant_time():
    import inspect

    from adminApi import router as admin_router

    src = inspect.getsource(admin_router.check_admin_key)
    assert "compare_digest" in src and "!=" not in src
    prior = os.environ.get("ADMIN_API_KEY")
    os.environ["ADMIN_API_KEY"] = "the-real-admin-key-123456"
    try:
        with harness() as h:
            c = h.client()
            assert c.get("/api/admin/users", headers={"X-Admin-Key": "wrong"}).status_code == 401
            assert c.get("/api/admin/users").status_code == 422
            ok = c.get("/api/admin/users", headers={"X-Admin-Key": "the-real-admin-key-123456"})
            assert ok.status_code == 200
            assert "angel_password" not in ok.text and USERS["A"]["pin"] not in ok.text
    finally:
        if prior is None:
            os.environ.pop("ADMIN_API_KEY", None)
        else:
            os.environ["ADMIN_API_KEY"] = prior
    print("ok  the admin key is compared in constant time and a wrong key is a 401")


if __name__ == "__main__":
    run_all(dict(globals()))
