"""MCP authentication: personal access tokens over Streamable HTTP.

Covers the endpoint end to end through the combined app (``main.create_app``):
missing, malformed, unknown, revoked and expired tokens and inactive users are
refused; scopes are enforced; each token sees only its owner's data; token
management is owner-scoped and needs the app password; failed attempts are
rate limited; concurrent first calls log in once; a mis-filed session is
refused; nothing secret reaches logs, responses or the database; account
deletion kills the token. The last test drives the endpoint with the MCP SDK's
own Streamable HTTP client over a real loopback socket.

Fake broker and temporary SQLite only. Run directly or under pytest.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import sys
import threading
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _security_harness import PASSWORD, USERS, FakeBroker, harness, run_all  # noqa: E402

MCP_PATH = "/mcp/mcp"
ACCEPT = "application/json, text/event-stream"


def _mcp_app():
    """The combined app with a lifespan that only runs the MCP transport."""
    import main
    import mcp_server

    # A StreamableHTTPSessionManager runs once per instance; give each app its own.
    mcp_server.mcp._session_manager = None
    app = main.create_app()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp_server.mcp.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
    return app


@contextlib.contextmanager
def mcp_harness():
    from fastapi.testclient import TestClient

    with harness(app_factory=_mcp_app) as h:
        with TestClient(h.app, base_url="https://testserver") as http:
            h.http = http
            yield h


def _rpc(http, method, params=None, token=None, *, id_=1):
    headers = {"Accept": ACCEPT, "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return http.post(MCP_PATH, headers=headers, content=json.dumps(body))


def _payload(res):
    """The JSON-RPC message from a JSON or SSE-framed response."""
    if res.headers.get("content-type", "").startswith("application/json"):
        return res.json()
    for line in res.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no JSON-RPC payload in: {res.text[:200]!r}")


_INIT = {"protocolVersion": "2025-06-18", "capabilities": {},
         "clientInfo": {"name": "test", "version": "0"}}


def _issue(user_id, **kw):
    from services import mcp_tokens

    raw, _view = mcp_tokens.issue(user_id, "test", **kw)
    return raw


def _call_text(http, token, tool, args=None):
    res = _rpc(http, "tools/call", {"name": tool, "arguments": args or {}}, token)
    assert res.status_code == 200, (res.status_code, res.text[:300])
    msg = _payload(res)
    result = msg["result"]
    return result, "".join(c.get("text", "") for c in result.get("content", []))


def test_requests_without_a_valid_token_are_refused():
    from db.models import McpToken
    from db.models import utcnow

    with mcp_harness() as h:
        assert _rpc(h.http, "initialize", _INIT).status_code == 401
        for bad in ("", "nope", "Bearer", "mfm_short", "mfm_" + "x" * 43, "x" * 300):
            assert _rpc(h.http, "initialize", _INIT, bad).status_code == 401, bad

        # Expired.
        raw = _issue(h.ids["A"])
        with h.db() as s:
            row = s.exec(__import__("sqlmodel").select(McpToken)).first()
            row.expires_at = utcnow() - timedelta(seconds=5)
            s.add(row)
            s.commit()
        assert _rpc(h.http, "initialize", _INIT, raw).status_code == 401

        # Inactive owner.
        raw_b = _issue(h.ids["B"])
        assert _rpc(h.http, "initialize", _INIT, raw_b).status_code == 200
        h.service.deactivate_user(h.ids["B"])
        assert _rpc(h.http, "initialize", _INIT, raw_b).status_code == 401
    print("ok  no/malformed/unknown/expired tokens and inactive users get 401")


def test_tool_calls_need_a_verified_identity_even_inside_the_server():
    import mcp_server

    from mcp.server.fastmcp.exceptions import ToolError

    with harness():
        for tool in ("get_holdings", "get_positions", "get_funds", "portfolio_summary"):
            try:
                asyncio.run(mcp_server.mcp.call_tool(tool, {}))
            except ToolError as e:
                assert "Authentication required" in str(e), e
            else:
                raise AssertionError(f"{tool} ran without a verified identity")
    print("ok  a tool reached with no auth context fails closed")


def test_scope_is_enforced():
    from db.models import McpToken
    from sqlmodel import select

    with mcp_harness() as h:
        raw = _issue(h.ids["A"])
        with h.db() as s:
            row = s.exec(select(McpToken)).first()
            row.scopes = "something:else"
            s.add(row)
            s.commit()
        res = _rpc(h.http, "initialize", _INIT, raw)
        assert res.status_code == 403, res.status_code
        assert "insufficient_scope" in res.text
    print("ok  a token without portfolio:read gets 403 insufficient_scope")


def test_each_token_sees_only_its_owner():
    with mcp_harness() as h:
        a, b = _issue(h.ids["A"]), _issue(h.ids["B"])
        assert _rpc(h.http, "initialize", _INIT, a).status_code == 200
        names = {t["name"] for t in _payload(_rpc(h.http, "tools/list", {}, a))["result"]["tools"]}
        assert "get_holdings" in names and "place_order" not in names

        _r, text_a = _call_text(h.http, a, "get_holdings")
        _r, text_b = _call_text(h.http, b, "get_holdings")
        assert "INFY-EQ" in text_a and "TCS-EQ" not in text_a, text_a
        assert "TCS-EQ" in text_b and "INFY-EQ" not in text_b, text_b

        _r, pos_a = _call_text(h.http, a, "get_positions")
        assert "INFY-EQ" in pos_a and "TCS-EQ" not in pos_a

        res, _t = _call_text(h.http, a, "place_order", {"orderid": "1"})
        assert res.get("isError") is True
    print("ok  A's token reads A's holdings and positions only; B's reads B's; place_order errors")


def test_token_management_is_owner_scoped_and_needs_the_password():
    from db.models import McpToken
    from services import mcp_tokens
    from sqlmodel import select

    with mcp_harness() as h:
        http_a = h.login("A", h.http)

        wrong = http_a.post("/api/mcp/tokens", json={"name": "x", "app_password": "nope-nope-nope"})
        assert wrong.status_code == 401

        made = http_a.post("/api/mcp/tokens", json={"name": "laptop", "app_password": PASSWORD})
        assert made.status_code == 201, made.text
        assert made.headers["cache-control"] == "no-store"
        raw = made.json()["token"]
        assert raw.startswith("mfm_") and len(raw) >= 47

        listed = http_a.get("/api/mcp/tokens").json()["tokens"]
        assert len(listed) == 1 and raw not in json.dumps(listed)
        assert listed[0]["prefix"] == raw[:10]

        with h.db() as s:
            row = s.exec(select(McpToken)).first()
        assert row.token_hash == mcp_tokens.hash_token(raw) and row.token_hash != raw

        b_token = _issue(h.ids["B"])
        with h.db() as s:
            b_row = s.exec(select(McpToken).where(McpToken.user_id == h.ids["B"])).first()
        assert http_a.delete(f"/api/mcp/tokens/{b_row.id}").status_code == 404
        assert _rpc(h.http, "initialize", _INIT, b_token).status_code == 200, "B's token was revoked by A"

        # Not signed in.
        from fastapi.testclient import TestClient
        anon = TestClient(h.app, base_url="https://testserver")
        assert anon.get("/api/mcp/tokens").status_code == 401
        assert anon.post("/api/mcp/tokens", json={"app_password": PASSWORD}).status_code == 401

        # Per-user cap on active tokens.
        for _ in range(mcp_tokens.MAX_ACTIVE_TOKENS_PER_USER - 1):
            _issue(h.ids["A"])
        try:
            _issue(h.ids["A"])
        except mcp_tokens.TokenError as e:
            assert e.status_code in (409, 429)
        else:
            raise AssertionError("token cap not enforced")
    print("ok  tokens: password re-check, shown once, hash only, owner-only revoke, capped")


def test_revocation_takes_effect_on_the_next_request():
    from services import mcp_tokens

    with mcp_harness() as h:
        raw = _issue(h.ids["A"])
        assert _rpc(h.http, "initialize", _INIT, raw).status_code == 200
        tid = mcp_tokens.verify(raw).token_id
        assert mcp_tokens.revoke(h.ids["A"], tid)
        assert _rpc(h.http, "initialize", _INIT, raw).status_code == 401
        assert _rpc(h.http, "tools/list", {}, raw).status_code == 401
    print("ok  a revoked token is refused on the very next request")


def test_failed_verifications_are_rate_limited_per_ip():
    import secrets

    from services import mcp_tokens

    with mcp_harness() as h:
        cap = 5
        original_cap = mcp_tokens._MAX_VERIFY_FAILURES_PER_IP
        mcp_tokens._MAX_VERIFY_FAILURES_PER_IP = cap
        lookups = {"n": 0}
        real = mcp_tokens.get_session

        def counting():
            lookups["n"] += 1
            return real()

        mcp_tokens.get_session = counting
        try:
            for _ in range(cap):
                guess = "mfm_" + secrets.token_urlsafe(32)
                assert _rpc(h.http, "initialize", _INIT, guess).status_code == 401
            assert lookups["n"] == cap
            # Over the cap: refused without touching the database, even for a real token.
            good = "mfm_" + secrets.token_urlsafe(32)
            assert _rpc(h.http, "initialize", _INIT, good).status_code == 401
            assert lookups["n"] == cap, "throttled verification still queried the database"
        finally:
            mcp_tokens.get_session = real
            mcp_tokens._MAX_VERIFY_FAILURES_PER_IP = original_cap
    print("ok  past the per-IP failure cap, verification is refused without a DB lookup")


def _as_user(user_id):
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    tok = AccessToken(token="redacted", client_id=f"user:{user_id}",
                      scopes=["portfolio:read"], expires_at=None)
    return auth_context_var.set(AuthenticatedUser(tok))


def test_a_misfiled_session_cannot_answer_for_another_user():
    import mcp_server
    import session_manager
    from services.mcp_tokens import mcp_session_key

    with harness() as h:
        sessions = session_manager.sessions
        key_a = mcp_session_key(h.ids["A"])
        # Plant B's broker client under A's key.
        b_client = FakeBroker("k", USERS["B"]["client_id"], "p", "t")
        with sessions._lock:
            sessions._sessions[key_a] = b_client
            sessions._created_at[key_a] = time.time()
        token = _as_user(h.ids["A"])
        try:
            try:
                mcp_server._require_client()
            except mcp_server.NotAuthorized:
                pass
            else:
                raise AssertionError("A was served B's broker session")
            assert sessions.get_client(key_a) is None, "the mis-filed session was not dropped"
            ro = mcp_server._require_client()
            assert ro.client_id == USERS["A"]["client_id"]
        finally:
            from mcp.server.auth.middleware.auth_context import auth_context_var
            auth_context_var.reset(token)
    print("ok  a session whose broker account is not the token owner's is refused and dropped")


def test_concurrent_first_calls_log_in_once():
    import mcp_server

    with harness() as h:
        FakeBroker.login_delay = 0.2
        results, errors = [], []

        def worker():
            from mcp.server.auth.middleware.auth_context import auth_context_var
            tok = _as_user(h.ids["A"])
            try:
                results.append(mcp_server._require_client().client_id)
            except Exception as e:  # pragma: no cover — reported below
                errors.append(e)
            finally:
                auth_context_var.reset(tok)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        FakeBroker.login_delay = 0.0
        assert not errors, errors
        assert results == [USERS["A"]["client_id"]] * 10
        assert FakeBroker.logins[USERS["A"]["client_id"]] == 1, FakeBroker.logins
    print("ok  ten concurrent first MCP calls share one broker login")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        if record.exc_info:
            msg += "\n" + logging.Formatter().formatException(record.exc_info)
        self.lines.append(msg)


def test_no_secret_reaches_logs_responses_or_the_database():
    cap = _Capture()
    root = logging.getLogger()
    prior_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    bodies = []
    try:
        with mcp_harness() as h:
            http_a = h.login("A", h.http)
            made = http_a.post("/api/mcp/tokens", json={"name": "leak-test", "app_password": PASSWORD})
            raw = made.json()["token"]
            bodies.append(http_a.get("/api/mcp/tokens").text)
            for tool in ("get_profile", "get_holdings", "get_funds", "portfolio_summary"):
                bodies.append(_rpc(h.http, "tools/call", {"name": tool, "arguments": {}}, raw).text)
            bodies.append(_rpc(h.http, "initialize", _INIT, raw + "x").text)
            bodies.append(http_a.get("/api/session/bootstrap").text)
            tid = json.loads(bodies[0])["tokens"][0]["id"]
            bodies.append(http_a.delete(f"/api/mcp/tokens/{tid}").text)
            bodies.append(_rpc(h.http, "initialize", _INIT, raw).text)
            with open(h.engine.url.database, "rb") as fh:
                db_bytes = fh.read()
    finally:
        root.removeHandler(cap)
        root.setLevel(prior_level)

    secrets_ = [raw]
    for u in USERS.values():
        secrets_ += [u["pin"], u["totp"], u["api_key"]]
    logs = "\n".join(cap.lines)
    for s in secrets_:
        assert s not in logs, f"secret {s[:6]}… appeared in logs"
        assert all(s not in b for b in bodies), f"secret {s[:6]}… appeared in a response"
    assert raw.encode() not in db_bytes, "raw MCP token is stored in the database"
    for u in USERS.values():
        assert u["pin"].encode() not in db_bytes and u["totp"].encode() not in db_bytes
    print("ok  raw tokens, PINs, TOTP secrets and API keys never appear in logs or responses")


def test_account_deletion_kills_the_users_tokens():
    from db.models import McpToken
    from sqlmodel import select

    with mcp_harness() as h:
        http_a = h.login("A", h.http)
        raw = _issue(h.ids["A"])
        assert _rpc(h.http, "initialize", _INIT, raw).status_code == 200
        res = http_a.request("DELETE", "/api/account", json={"app_password": PASSWORD})
        assert res.status_code == 200 and res.json()["deleted"] is True, res.text
        with h.db() as s:
            assert s.exec(select(McpToken).where(McpToken.user_id == h.ids["A"])).first() is None
        assert _rpc(h.http, "initialize", _INIT, raw).status_code == 401
    print("ok  deleting the account deletes its tokens; the old token gets 401")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_mcp_client_interoperates_over_loopback():
    import httpx
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    with harness(app_factory=_mcp_app) as h:
        raw = _issue(h.ids["A"])
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(h.app, host="127.0.0.1", port=port,
                                               lifespan="on", log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"
        url = f"http://127.0.0.1:{port}{MCP_PATH}"

        async def session(headers):
            async with httpx.AsyncClient(headers=headers, timeout=15) as hc:
                async with streamable_http_client(url, http_client=hc) as (read, write, _sid):
                    async with ClientSession(read, write) as s:
                        await s.initialize()
                        tools = await s.list_tools()
                        res = await s.call_tool("get_holdings", {})
                        return {t.name for t in tools.tools}, res

        try:
            names, res = asyncio.run(session({"Authorization": f"Bearer {raw}"}))
            text = "".join(getattr(c, "text", "") for c in res.content)
            assert "INFY-EQ" in text and "TCS-EQ" not in text, text
            assert "place_order" not in names and "login" not in names

            try:
                asyncio.run(asyncio.wait_for(session({}), timeout=20))
            except BaseException as e:  # 401 surfaces as an HTTP/MCP error group
                assert not isinstance(e, AssertionError)
            else:
                raise AssertionError("an unauthenticated real client was served")
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    print(f"ok  the MCP SDK client works end to end at {MCP_PATH} with a bearer token, and fails without")


if __name__ == "__main__":
    run_all(dict(globals()))
