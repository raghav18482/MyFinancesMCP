"""Tests for registering without a broker and connecting one later.

Covers the two levels of access: an app account (phone number and password,
learning only) and a brokerage connection (Angel One credentials proved with
the broker, then stored encrypted on the same ``users`` row).

Reuses the harness from ``test_spa_api``: the real app over a temp sqlite file,
with a fake broker that accepts, rejects (``broker["accepts"] = False``) or
cannot be reached (``broker["reachable"] = False``).

Run directly (``python tests/test_brokerage_registration.py``) or under pytest.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_spa_api import GOOD_PASSWORD, NUMBER, harness  # noqa: E402

OTHER_NUMBER = "+919811112222"
CONNECT = {
    "client_id": "ZX9876",
    "api_key": "NEWAPIKEY",
    "password": "8642",
    "totp_secret": "KRSXG5CTMVRXEZLU",
}
CONNECT_SECRETS = ("NEWAPIKEY", "8642", "KRSXG5CTMVRXEZLU")
ENROLLED_SECRETS = ("APIKEY123", "4321", "JBSWY3DPEHPK3PXP")
CONNECT_MESSAGE = "Please connect your brokerage account to access this feature."


def _register(h, number="9876543210", **extra):
    return h.http.post("/api/auth/enroll", json={
        "country_code": "91",
        "whatsapp_number": number,
        "app_password": GOOD_PASSWORD,
        "app_password_confirm": GOOD_PASSWORD,
        **extra,
    })


def _login(h, number="9876543210"):
    return h.http.post("/api/auth/login", json={
        "country_code": "91", "whatsapp_number": number, "app_password": GOOD_PASSWORD,
    })


def _row(h, number=NUMBER):
    from sqlmodel import select

    from db.models import User

    with h.service.get_session() as s:
        return s.exec(select(User).where(User.whatsapp_number == number)).first()


def _assert_no_secrets(text, secrets=CONNECT_SECRETS + ENROLLED_SECRETS):
    for secret in secrets:
        # A four-digit PIN is a substring of the test phone number, so short
        # values are matched as whole JSON strings; long ones anywhere.
        needle = f'"{secret}"' if len(secret) < 6 else secret
        assert needle not in text, f"a credential leaked into a response: {secret!r}"


# ── Registration ──────────────────────────────────────────────────────────


def test_register_with_phone_and_password_only():
    with harness() as h:
        res = _register(h)
        assert res.status_code == 200, res.text
        body = res.json()
        me = h.bootstrap()
        row = _row(h)

    assert body["ok"] is True
    assert body["phone_number"] == NUMBER and body["user_id"] == row.id
    assert body["brokerage_status"] == "not_connected"
    assert body["client_id"] is None
    assert body["available_features"] == ["learning"]

    assert me["authenticated"] is True and me["brokerage_status"] == "not_connected"
    assert me["premium"] == {"registered": False, "whatsapp_number": None}

    for col in ("angel_api_key", "angel_client_id", "angel_password_encrypted",
                "angel_totp_secret_encrypted", "angel_access_token"):
        assert getattr(row, col) is None, f"{col} should stay NULL until a broker is connected"
    assert row.brokerage_status == "not_connected"
    assert row.app_password_hash.startswith("scrypt$")
    assert GOOD_PASSWORD not in row.app_password_hash
    assert not h.live, "registering without a broker must not contact one"
    print("ok  phone + password registers a user with every broker column NULL")


def test_register_accepts_phone_number_alias_and_no_confirmation():
    with harness() as h:
        res = h.http.post("/api/auth/enroll", json={
            "country_code": "91", "phone_number": "9876543210", "app_password": GOOD_PASSWORD,
        })
    assert res.status_code == 200, res.text
    assert res.json()["phone_number"] == NUMBER
    print("ok  phone_number is accepted as an alias, confirmation is optional over the API")


def test_duplicate_number_is_rejected():
    with harness() as h:
        assert _register(h).status_code == 200
        h.http.post("/api/auth/logout")
        before = _row(h).app_password_hash

        res = _register(h, app_password="another-password-x", app_password_confirm="another-password-x")
        after = _row(h).app_password_hash

    assert res.status_code == 409, res.text
    assert res.json()["field"] == "whatsapp_number"
    assert before == after, "a refused registration must not touch the existing row"
    print("ok  a duplicate number is a 409 and the existing account is untouched")


def test_duplicate_of_a_connected_account_is_rejected():
    with harness() as h:
        h.enrol_row()
        res = _register(h)
    assert res.status_code == 409, res.text
    print("ok  a number already used by an enrolled (connected) user is refused")


def test_partial_broker_fields_are_rejected():
    with harness() as h:
        res = _register(h, client_id="AB1234", api_key="K")
        assert res.status_code == 400, res.text
        assert res.json()["field"] in ("password", "totp_secret")
        assert _row(h) is None, "a half-filled form must not create an account"
    print("ok  some-but-not-all broker fields is a 400 and creates nothing")


def test_short_password_is_rejected():
    with harness() as h:
        res = _register(h, app_password="short", app_password_confirm="short")
        assert res.status_code == 400
        assert _row(h) is None
    print("ok  the app password rule applies to phone-only registration")


# ── Login ─────────────────────────────────────────────────────────────────


def test_user_without_broker_can_log_in_and_no_broker_is_contacted():
    with harness() as h:
        _register(h)
        h.http.post("/api/auth/logout")

        res = _login(h)
        me = h.bootstrap()

        assert res.status_code == 200, res.text
        body = res.json()
        assert body["brokerage_status"] == "not_connected"
        assert body["available_features"] == ["learning"]
        assert me["authenticated"] is True
        assert not h.live, "login must not start a broker session"
    print("ok  a user with no broker logs in; nothing contacts Angel One")


def test_existing_enrolled_user_still_logs_in_and_keeps_brokerage():
    with harness() as h:
        h.enrol_row()
        res = _login(h)
        assert res.status_code == 200, res.text
        assert res.json()["available_features"] == ["learning", "brokerage"]
        assert h.http.get("/api/portfolio/positions").status_code == 200
    print("ok  an enrolled user logs in as before and keeps brokerage access")


def test_login_works_while_the_broker_is_down():
    with harness() as h:
        h.enrol_row()
        h.broker["reachable"] = False
        assert _login(h).status_code == 200
        assert h.bootstrap()["authenticated"] is True
    print("ok  login does not depend on Angel One being up")


# ── Connecting a broker ───────────────────────────────────────────────────


def test_connect_requires_a_signed_in_user():
    with harness() as h:
        res = h.http.post("/api/brokerage/register", json=CONNECT)
        status = h.http.get("/api/brokerage/status")
    assert res.status_code == 401 and res.json()["code"] == "not_authenticated"
    assert status.status_code == 401
    print("ok  brokerage endpoints refuse anonymous requests with 401")


def test_connect_links_proven_credentials_encrypted():
    with harness() as h:
        _register(h)
        res = h.http.post("/api/brokerage/register", json=CONNECT)
        assert res.status_code == 200, res.text
        row = _row(h)
        me = h.bootstrap()
        status = h.http.get("/api/brokerage/status").json()
        positions = h.http.get("/api/portfolio/positions")
        creds = h.service.credentials_for_user(row.id)

    body = res.json()
    assert body["brokerage_status"] == "connected" and body["is_connected"] is True
    assert body["provider"] == "angel_one"
    assert body["available_features"] == ["learning", "brokerage"]
    _assert_no_secrets(res.text)

    assert row.brokerage_status == "connected"
    assert row.angel_client_id == "ZX9876"
    assert row.angel_api_key != "NEWAPIKEY", "API key must be encrypted at rest"
    assert row.angel_password_encrypted != "8642"
    assert row.angel_totp_secret_encrypted != "KRSXG5CTMVRXEZLU"
    assert (creds.api_key, creds.password, creds.totp_secret) == CONNECT_SECRETS

    assert me["brokerage_status"] == "connected" and me["client_id"] == "ZX9876"
    assert status == {"brokerage_status": "connected", "is_connected": True, "provider": "angel_one"}
    assert positions.status_code == 200, positions.text
    print("ok  connecting proves the credentials, stores them encrypted, unlocks brokerage")


def test_rejected_credentials_do_not_connect():
    with harness() as h:
        _register(h)
        h.broker["accepts"] = False
        res = h.http.post("/api/brokerage/register", json=CONNECT)
        row = _row(h)
        positions = h.http.get("/api/portfolio/positions")

    assert res.status_code == 400, res.text
    assert res.json()["code"] == "brokerage_auth_failed"
    _assert_no_secrets(res.text)
    assert row.brokerage_status == "failed"
    assert row.angel_client_id is None and row.angel_password_encrypted is None
    assert positions.status_code == 403
    assert not h.live, "a rejected attempt must not leave a broker session behind"
    print("ok  rejected credentials store nothing and do not grant brokerage access")


def test_unreachable_broker_changes_nothing():
    with harness() as h:
        _register(h)
        h.broker["reachable"] = False
        res = h.http.post("/api/brokerage/register", json=CONNECT)
        row = _row(h)
    assert res.status_code == 502 and res.json()["code"] == "brokerage_unavailable"
    assert row.brokerage_status == "not_connected", "an outage says nothing about the credentials"
    print("ok  an unreachable broker leaves the status and the row alone")


def test_failed_reconnect_keeps_the_working_link():
    with harness() as h:
        h.enrol_row()
        _login(h)
        before = _row(h)
        h.broker["accepts"] = False
        res = h.http.post("/api/brokerage/register", json=CONNECT)
        after = _row(h)
        h.broker["accepts"] = True
        positions = h.http.get("/api/portfolio/positions")

    assert res.status_code == 400
    for col in ("angel_api_key", "angel_client_id", "angel_password_encrypted",
                "angel_totp_secret_encrypted"):
        assert getattr(after, col) == getattr(before, col), f"{col} was overwritten by a failed attempt"
    assert after.brokerage_status == "connected"
    assert positions.status_code == 200
    print("ok  a failed reconnect never overwrites credentials that work")


def test_reconnecting_replaces_an_expired_link():
    with harness() as h:
        h.enrol_row()
        _login(h)
        h.service.set_brokerage_status(_row(h).id, "expired")
        assert h.http.get("/api/portfolio/positions").status_code == 403
        res = h.http.post("/api/brokerage/register", json=CONNECT)
        row = _row(h)
        positions = h.http.get("/api/portfolio/positions")
    assert res.status_code == 200, res.text
    assert row.brokerage_status == "connected" and row.angel_client_id == "ZX9876"
    assert positions.status_code == 200
    print("ok  reconnecting an expired link stores the new credentials and restores access")


def test_an_account_linked_elsewhere_is_refused():
    with harness() as h:
        h.enrol_row(angel_client_id="ZX9876")
        _register(h, number="9811112222")
        res = h.http.post("/api/brokerage/register", json=CONNECT)
        mine = _row(h, OTHER_NUMBER)
        theirs = _row(h)

    assert res.status_code == 409, res.text
    assert res.json()["field"] == "client_id"
    assert mine.angel_client_id is None and mine.brokerage_status == "not_connected"
    assert theirs.angel_client_id == "ZX9876" and theirs.brokerage_status == "connected"
    print("ok  a broker account linked to another user is a 409; neither row changes")


def test_a_user_id_in_the_body_is_ignored():
    with harness() as h:
        victim = h.enrol_row()
        _register(h, number="9811112222")
        res = h.http.post("/api/brokerage/register", json={**CONNECT, "user_id": victim["id"]})
        mine = _row(h, OTHER_NUMBER)
        theirs = _row(h)

    assert res.status_code == 200, res.text
    assert mine.angel_client_id == "ZX9876", "the link belongs to the signed-in user"
    assert theirs.angel_client_id == "AB1234", "the other user's row must be untouched"
    print("ok  the identity comes from the cookie; a user_id in the body changes nothing")


def test_missing_fields_are_rejected_before_contacting_the_broker():
    with harness() as h:
        _register(h)
        res = h.http.post("/api/brokerage/register", json={**CONNECT, "totp_secret": ""})
        assert res.status_code == 400 and res.json()["field"] == "totp_secret"
        assert not h.live
    print("ok  a missing broker field is a 400 and the broker is not called")


def test_connect_attempts_are_throttled():
    from services import login_throttle

    with harness() as h:
        _register(h)
        h.broker["accepts"] = False
        for _ in range(login_throttle._MAX_PER_IDENTIFIER):
            assert h.http.post("/api/brokerage/register", json=CONNECT).status_code == 400
        res = h.http.post("/api/brokerage/register", json=CONNECT)
    assert res.status_code == 429 and int(res.headers["retry-after"]) > 0
    print("ok  repeated connect attempts are throttled")


# ── Feature authorization ─────────────────────────────────────────────────

_BROKERAGE_GETS = (
    "/api/portfolio/positions",
    "/api/portfolio/orders",
    "/api/portfolio/analytics",
    "/api/research/fundamental?symbol=INFY",
    "/api/sectors/breadth",
    "/api/news/portfolio",
    "/api/trading/proposals",
    "/api/premium/status",
    "/api/agent/threads",
)


def test_brokerage_endpoints_refuse_users_without_a_broker():
    with harness() as h:
        _register(h)
        answers = {path: h.http.get(path) for path in _BROKERAGE_GETS}
        answers["POST /api/ai/ask"] = h.http.post("/api/ai/ask", json={"question": "hi"})
        answers["POST /api/mcp/tokens"] = h.http.post("/api/mcp/tokens", json={"app_password": GOOD_PASSWORD})
        still_signed_in = h.bootstrap()["authenticated"]
        tokens = h.http.get("/api/mcp/tokens")

    for path, res in answers.items():
        assert res.status_code == 403, f"{path}: {res.status_code} {res.text}"
        assert res.json() == {"error": CONNECT_MESSAGE, "detail": CONNECT_MESSAGE,
                              "code": "brokerage_not_connected"}, path
    assert still_signed_in, "a 403 for a missing broker must not sign the user out"
    assert tokens.status_code == 200, "listing MCP tokens needs only an app account"
    print(f"ok  {len(answers)} brokerage endpoints answer 403 'connect your brokerage'")


def test_learning_and_account_endpoints_need_no_broker():
    with harness() as h:
        _register(h)
        status = h.http.get("/api/brokerage/status")
        learn = h.http.get("/learn")
        page = h.http.get("/dashboard", follow_redirects=False)

    assert status.status_code == 200
    assert status.json() == {"brokerage_status": "not_connected", "is_connected": False, "provider": None}
    assert learn.status_code == 200, "the learning page needs a signed-in user, not a broker"
    assert page.status_code == 302 and page.headers["location"] == "/app/connect-brokerage"
    print("ok  learning and account pages work without a broker; broker pages redirect to connect")


def test_rejected_rebuild_expires_the_link_and_asks_to_reconnect():
    with harness() as h:
        h.enrol_row()
        _login(h)
        h.broker["accepts"] = False
        res = h.http.get("/api/portfolio/positions")
        row = _row(h)
        me = h.bootstrap()

    assert res.status_code == 403 and res.json()["code"] == "brokerage_reauth_required"
    assert row.brokerage_status == "expired"
    assert me["authenticated"] is True and me["available_features"] == ["learning"]
    print("ok  credentials the broker now rejects: link expired, user asked to reconnect")


def test_unreachable_broker_on_rebuild_is_a_503_and_keeps_the_link():
    with harness() as h:
        h.enrol_row()
        _login(h)
        h.broker["reachable"] = False
        res = h.http.get("/api/portfolio/positions")
        row = _row(h)
        h.broker["reachable"] = True
        later = h.http.get("/api/portfolio/positions")

    assert res.status_code == 503 and res.json()["code"] == "brokerage_unavailable"
    assert row.brokerage_status == "connected", "an outage must not expire the link"
    assert later.status_code == 200
    print("ok  a broker outage is a 503 and the link survives it")


def test_disconnect_wipes_credentials_and_removes_access():
    with harness() as h:
        h.enrol_row()
        _login(h)
        assert h.http.get("/api/portfolio/positions").status_code == 200
        assert h.live

        res = h.http.post("/api/brokerage/disconnect")
        row = _row(h)
        positions = h.http.get("/api/portfolio/positions")
        me = h.bootstrap()

        # The freed client id can be linked again.
        relink = h.http.post("/api/brokerage/register", json={**CONNECT, "client_id": "AB1234"})

    assert res.status_code == 200, res.text
    assert res.json()["brokerage_status"] == "disconnected"
    for col in ("angel_api_key", "angel_client_id", "angel_password_encrypted",
                "angel_totp_secret_encrypted"):
        assert getattr(row, col) is None, f"{col} survived a disconnect"
    assert positions.status_code == 403 and positions.json()["code"] == "brokerage_reauth_required"
    assert me["authenticated"] is True and me["brokerage_status"] == "disconnected"
    assert relink.status_code == 200, relink.text
    print("ok  disconnect wipes the credentials and ends brokerage access, not the account")


def test_account_without_broker_can_be_deleted():
    from services import account_deletion

    with harness() as h:
        original = account_deletion.get_session
        account_deletion.get_session = h.service.get_session
        try:
            _register(h)
            res = h.http.request("DELETE", "/api/account", json={"app_password": GOOD_PASSWORD})
            assert res.status_code == 200, res.text
            assert res.json() == {"ok": True, "deleted": True}
            assert _row(h) is None
        finally:
            account_deletion.get_session = original
    print("ok  an account that never connected a broker can be deleted")


def test_no_response_carries_a_credential():
    with harness() as h:
        h.enrol_row()
        texts = [_login(h).text, h.http.get("/api/session/bootstrap").text,
                 h.http.get("/api/brokerage/status").text]
        h.http.post("/api/auth/logout")
        _register(h, number="9811112222")
        texts.append(h.http.post("/api/brokerage/register", json=CONNECT).text)
        texts.append(h.http.get("/api/session/bootstrap").text)
    for text in texts:
        _assert_no_secrets(text)
        assert "angel_api_key" not in text and "encrypted" not in text
    print("ok  login, bootstrap, status and connect responses carry no credential")


# ── MCP and the scheduler ─────────────────────────────────────────────────


def test_mcp_tools_refuse_a_user_without_a_broker():
    import mcp_server

    with harness() as h:
        _register(h)
        uid = _row(h).id
        original = mcp_server._verified_user_id
        mcp_server._verified_user_id = lambda: uid
        try:
            mcp_server._require_client()
        except mcp_server.NotAuthorized as e:
            assert "Connect" in str(e)
        else:
            raise AssertionError("MCP served a user with no connected broker")
        finally:
            mcp_server._verified_user_id = original
    print("ok  MCP tools refuse a user whose broker is not connected")


def test_scheduler_skips_users_without_a_broker():
    from datetime import datetime, timezone

    from services.schedular import job_executor, repository

    with harness() as h:
        _register(h)
        uid = _row(h).id
        logs, finished, created = [], [], []
        patches = {
            (job_executor, "get_session"): h.service.get_session,
            (job_executor, "_write_log"): lambda claim, **kw: logs.append(kw),
            (repository, "finish_schedule"): lambda *a, **kw: finished.append(kw),
        }
        originals = {k: getattr(*k) for k in patches}
        for (mod, name), value in patches.items():
            setattr(mod, name, value)
        original_create = job_executor.sessions.create_session
        job_executor.sessions.create_session = lambda *a, **kw: created.append(a)
        try:
            claim = repository.ClaimedSchedule(
                id=1, user_id=uid, kind="daily_briefing", interval_minutes=1440,
                next_run=datetime.now(timezone.utc),
            )
            asyncio.run(job_executor.run_one(claim))
        finally:
            for (mod, name), value in originals.items():
                setattr(mod, name, value)
            job_executor.sessions.create_session = original_create

    assert not created, "no broker session for a user without a broker"
    assert logs and logs[0]["status"] == "skipped"
    assert finished, "the schedule is re-armed for its next tick"
    print("ok  the briefing scheduler skips users with no connected broker")


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
    print(f"\n{passed} tests passed")
