"""Broker logins off the event loop (web/broker_sessions.py, web/dependencies.py).

Covers single-flight per user, concurrency across users and its bound,
cancellation, failed-login cleanup, runtime lifecycle across event loops,
stale memo entries, event-loop responsiveness during a login, and releasing
every device's sessions on logout and account deletion.

Run directly: ``python tests/test_broker_concurrency.py``.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _security_harness import FakeBroker, USERS, harness  # noqa: E402

BASE = "https://testserver"


def _live_sids():
    import session_manager

    return [k for k in session_manager.sessions._sessions if not k.startswith("mcp-user-")]


def _expire_all_web_sessions():
    import session_manager

    for sid in _live_sids():
        session_manager.sessions.remove_session(sid)


@contextlib.contextmanager
def _concurrency_probe():
    """Wraps FakeBroker logins to record the most that ran at the same time."""
    state = {"active": 0, "max": 0}
    lock = threading.Lock()
    original = FakeBroker.ensure_session

    def ensure_session(self):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        try:
            return original(self)
        finally:
            with lock:
                state["active"] -= 1

    with mock.patch.object(FakeBroker, "ensure_session", ensure_session):
        yield state


async def _async_client(app, cookie=None):
    import httpx

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
    if cookie:
        client.cookies.set("session", cookie)
    return client


# ── through the app, on one event loop ───────────────────────────────────


def test_same_user_concurrent_requests_share_one_login():
    with harness() as h:
        cookie = h.login("A").cookies.get("session")
        _expire_all_web_sessions()
        before = FakeBroker.logins[USERS["A"]["client_id"]]
        FakeBroker.login_delay = 0.3

        async def run():
            from web import broker_sessions

            broker_sessions.reset_for_tests()
            client = await _async_client(h.app, cookie)
            async with client:
                return await asyncio.gather(*[client.get("/api/portfolio/positions") for _ in range(20)])

        try:
            responses = asyncio.run(run())
        finally:
            FakeBroker.login_delay = 0.0
        assert [r.status_code for r in responses] == [200] * 20
        assert FakeBroker.logins[USERS["A"]["client_id"]] == before + 1
        assert len(_live_sids()) == 1


def test_different_users_log_in_concurrently_within_the_bound():
    with harness() as h:
        cookies = {who: h.login(who).cookies.get("session") for who in ("A", "B")}
        _expire_all_web_sessions()
        FakeBroker.login_delay = 0.3

        async def run(limit):
            from web import broker_sessions

            broker_sessions.reset_for_tests()
            with mock.patch.dict(os.environ, {"BROKER_LOGIN_CONCURRENCY": str(limit)}):
                broker_sessions.get_runtime()
            clients = {who: await _async_client(h.app, c) for who, c in cookies.items()}
            try:
                results = await asyncio.gather(
                    *[clients[who].get("/api/session/bootstrap") for who in ("A", "B") for _ in range(3)]
                )
            finally:
                for c in clients.values():
                    await c.aclose()
            return results

        try:
            with _concurrency_probe() as probe:
                results = asyncio.run(run(1))
            assert probe["max"] == 1, probe
            ids = [r.json()["client_id"] for r in results]
            assert ids == [USERS["A"]["client_id"]] * 3 + [USERS["B"]["client_id"]] * 3, ids

            _expire_all_web_sessions()
            import web.broker_sessions as bs
            bs._recent_logins.clear()
            with _concurrency_probe() as probe:
                asyncio.run(run(4))
            assert probe["max"] == 2, "two users log in at the same time when allowed"
        finally:
            FakeBroker.login_delay = 0.0


def test_event_loop_stays_responsive_during_a_login():
    with harness() as h:
        cookie = h.login("A").cookies.get("session")
        _expire_all_web_sessions()
        FakeBroker.login_delay = 1.5

        async def run():
            from web import broker_sessions

            broker_sessions.reset_for_tests()
            gaps = []

            async def ticker():
                last = time.monotonic()
                for _ in range(12):
                    await asyncio.sleep(0.1)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now

            async with await _async_client(h.app, cookie) as a, await _async_client(h.app) as b:
                slow = asyncio.create_task(a.get("/api/portfolio/positions"))
                await asyncio.sleep(0.2)
                started = time.monotonic()
                fast = await b.get("/api/auth/config")
                fast_latency = time.monotonic() - started
                await ticker()
                return (await slow).status_code, fast.status_code, fast_latency, max(gaps)

        try:
            slow, fast, latency, worst_gap = asyncio.run(run())
        finally:
            FakeBroker.login_delay = 0.0
        assert slow == 200 and fast == 200
        assert latency < 0.2, f"/api/auth/config took {latency:.3f}s during a login"
        assert worst_gap < 0.15, f"event loop stalled for {worst_gap:.3f}s"


def test_logout_releases_every_devices_session():
    import session_manager

    with harness() as h:
        h.login("A")
        h.login("A")
        h.login("B")
        a_sessions = lambda: [s for s, c in session_manager.sessions._sessions.items()
                              if c.client_id == USERS["A"]["client_id"] and not s.startswith("mcp-user-")]
        assert len(a_sessions()) >= 1
        device = h.login("A")
        device.post("/api/auth/logout")
        assert a_sessions() == []
        assert any(c.client_id == USERS["B"]["client_id"] for c in session_manager.sessions._sessions.values())


def test_account_deletion_releases_every_devices_session():
    import session_manager
    from _security_harness import PASSWORD

    with harness() as h:
        h.login("A")
        device = h.login("A")
        res = device.request("DELETE", "/api/account", json={"app_password": PASSWORD})
        assert res.status_code == 200 and res.json()["deleted"] is True, res.text
        assert not any(c.client_id == USERS["A"]["client_id"]
                       for c in session_manager.sessions._sessions.values())


def test_a_rebuild_queues_the_old_session_for_release():
    import session_manager

    with harness() as h:
        device = h.login("A")
        old_sid = _live_sids()[0]
        session_manager.sessions._created_at[old_sid] = 1.0  # expired long ago
        assert device.get("/api/portfolio/positions").status_code == 200
        assert old_sid in session_manager.sessions.cleanup_expired()


# ── the runtime directly ─────────────────────────────────────────────────


def _creds(client_id="CC3333", password="pw"):
    return SimpleNamespace(api_key="key", client_id=client_id, password=password, totp_secret="totp")


@contextlib.contextmanager
def _fake_sessions(delay=0.0, fail=None):
    """Replace session_manager.sessions.create_session with a slow fake."""
    import session_manager

    created, removed = [], []
    live = {}

    def create(sid, api_key, client_id, password, totp_secret):
        time.sleep(delay)
        if fail:
            raise fail
        client = SimpleNamespace(client_id=client_id)
        created.append(sid)
        live[sid] = client
        return client

    def remove(sid):
        removed.append(sid)
        live.pop(sid, None)

    s = session_manager.sessions
    with mock.patch.object(s, "create_session", create), \
            mock.patch.object(s, "remove_session", remove), \
            mock.patch.object(s, "get_client", live.get):
        yield SimpleNamespace(created=created, removed=removed, live=live)


def _reset():
    from web import broker_sessions

    broker_sessions.reset_for_tests()


def test_cancelling_one_caller_leaves_the_login_running_for_others():
    from web import broker_sessions

    _reset()

    async def run():
        rt = broker_sessions.get_runtime()
        first = asyncio.create_task(rt.login("user:1", _creds))
        second = asyncio.create_task(rt.login("user:1", _creds))
        await asyncio.sleep(0.05)
        first.cancel()
        result = await second
        return first, result

    with _fake_sessions(delay=0.3) as fake:
        first, (sid, client) = asyncio.run(run())
    assert first.cancelled()
    assert fake.created == [sid] and client.client_id == "CC3333"


def test_all_callers_cancelled_and_the_login_fails_cleanly():
    from web import broker_sessions

    _reset()
    handled = []

    async def run():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: handled.append(ctx))
        rt = broker_sessions.get_runtime()
        caller = asyncio.create_task(rt.login("user:2", _creds))
        await asyncio.sleep(0.05)
        caller.cancel()
        await asyncio.sleep(0.5)  # let the background login finish and fail
        return rt

    with _fake_sessions(delay=0.2, fail=RuntimeError("Invalid totp")) as fake:
        rt = asyncio.run(run())
    import gc

    gc.collect()  # "exception was never retrieved" is reported on collection
    assert rt._inflight == {}, rt._inflight
    assert handled == [], handled
    assert fake.created == []
    assert rt.login_limiter.borrowed_tokens == 0


def test_all_callers_cancelled_and_the_login_succeeds():
    from web import broker_sessions

    _reset()

    async def run():
        rt = broker_sessions.get_runtime()
        caller = asyncio.create_task(rt.login("user:3", _creds))
        await asyncio.sleep(0.05)
        caller.cancel()
        await asyncio.sleep(0.4)
        # A later login for the same user reuses the session made in the background.
        return await rt.login("user:3", _creds)

    with _fake_sessions(delay=0.2) as fake:
        sid, _client = asyncio.run(run())
    assert fake.created == [sid], "registered once and reused"


def test_reuse_requires_identical_credentials():
    from web import broker_sessions

    _reset()

    async def run():
        rt = broker_sessions.get_runtime()
        good = await rt.login("enroll:CC3333", lambda: _creds(password="right"))
        other = await rt.login("enroll:CC3333", lambda: _creds(password="wrong"))
        return good, other

    with _fake_sessions() as fake:
        (sid1, _), (sid2, _) = asyncio.run(run())
    assert sid1 != sid2 and fake.created == [sid1, sid2], \
        "a caller with different secrets must never receive someone's live session"


def test_runtimes_are_per_loop_and_stop_cleanly():
    from web import broker_sessions

    _reset()
    holder = {}

    async def first_loop():
        rt = broker_sessions.get_runtime()
        holder["rt"] = rt
        async with rt.login_limiter:  # built inside this loop: usable here
            pass
        return rt

    async def second_loop():
        rt = broker_sessions.get_runtime()
        old = holder["rt"]
        assert rt is not old and not old.running
        async with rt.login_limiter:
            pass
        await rt.stop()
        try:
            await rt.login("user:9", _creds)
        except broker_sessions.BrokerRuntimeUnavailable:
            pass
        else:
            raise AssertionError("a stopped runtime accepted a login")
        assert broker_sessions.get_runtime() is not rt, "a stopped runtime is replaced"

    asyncio.run(first_loop())
    asyncio.run(second_loop())


def test_a_login_finishing_after_stop_keeps_nothing():
    from web import broker_sessions

    _reset()

    async def run():
        rt = broker_sessions.get_runtime()
        caller = asyncio.create_task(rt.login("user:4", _creds))
        await asyncio.sleep(0.05)
        await rt.stop()
        try:
            await caller
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.4)

    with _fake_sessions(delay=0.2) as fake:
        asyncio.run(run())
    assert fake.live == {}, "a session made after stop() must not be kept"


def test_memo_entries_go_stale():
    from web.broker_sessions import BrokerRuntime

    now = [0.0]
    rt = BrokerRuntime(clock=lambda: now[0])
    with _fake_sessions() as fake:
        fake.live["s1"] = SimpleNamespace(client_id="AA1111")
        rt.remember(1, "s1", "AA1111", session_version=3)
        assert rt.recall(1, "AA1111", 3)[0] == "s1"
        assert rt.recall(1, "AA1111", 4) is None, "logout elsewhere moved the version"
        rt.remember(1, "s1", "AA1111", 3)
        fake.live.pop("s1")
        assert rt.recall(1, "AA1111", 3) is None, "the session is gone"
        fake.live["s2"] = SimpleNamespace(client_id="AA1111")
        rt.remember(1, "s2", "AA1111", 3)
        now[0] = 31
        assert rt.recall(1, "AA1111", 3) is None, "expired"
        rt.remember(1, "s2", "AA1111", 3)
        rt.forget_user(1)
        assert rt.recall(1, "AA1111", 3) is None
        rt.remember(2, "s2", "AA1111", 3)
        now[0] = 100
        rt.purge_memo()
        assert rt._memo == {}


def test_expired_on_access_sessions_are_queued_for_release():
    import session_manager

    sm = session_manager.SessionManager()
    sm._sessions["web1"] = SimpleNamespace(client_id="X")
    sm._created_at["web1"] = 1.0  # long expired (0 would read as "no timestamp")
    assert sm.get_client("web1") is None
    sm.mark_for_release("web2")
    assert sm.cleanup_expired() == ["web1", "web2"]
    assert sm.cleanup_expired() == []



def test_mcp_tools_run_in_threads_with_unchanged_schemas():
    """Wrapping the tools for worker threads must not change what clients see."""
    import inspect

    import mcp_server
    from mcp.server.fastmcp.tools.base import Tool

    tools = mcp_server.mcp._tool_manager.list_tools()
    assert len(tools) == len(mcp_server.ALLOWED_MCP_TOOLS)
    for t in tools:
        assert t.is_async and inspect.iscoroutinefunction(t.fn), t.name
        original = Tool.from_function(t.fn.__wrapped__, name=t.name, title=t.title,
                                      annotations=t.annotations)
        for attr in ("parameters", "context_kwarg", "description", "title", "annotations"):
            assert getattr(original, attr) == getattr(t, attr), (t.name, attr)

if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
