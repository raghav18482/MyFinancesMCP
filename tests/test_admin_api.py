"""The admin API: who can reach it, what it returns, and how failures are limited.

Run directly: ``python tests/test_admin_api.py``.
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _security_harness import USERS, harness  # noqa: E402

KEY = "k" * 16 + "-admin-key-for-tests-0123"   # 41 characters
LOOPBACK = ("127.0.0.1", 50000)
S_PIN = "8364"
S_TOTP = "JBSWY3DPEHPK3PXPJBSWY3DPEH"


def _client(app, peer=LOOPBACK):
    from fastapi.testclient import TestClient

    return TestClient(app, base_url="https://testserver", client=peer)


def _env(**values):
    base = {"ADMIN_API_KEY": KEY}
    base.update(values)
    return mock.patch.dict(os.environ, base)


def _reset_guard():
    from adminApi.guard import guard

    guard.reset()


def test_responses_carry_no_api_key():
    _reset_guard()
    with _env(), harness() as h:
        c = _client(h.app)
        res = c.get("/api/admin/users", headers={"X-Admin-Key": KEY})
        assert res.status_code == 200, res.text
        assert "angel_api_key" not in res.text
        assert USERS["A"]["api_key"] not in res.text
        assert all("has_broker_credentials" in u for u in res.json())


def test_validation_errors_never_echo_the_body():
    _reset_guard()
    with _env(), harness() as h:
        c = _client(h.app)
        # Missing fields: FastAPI's default 422 would echo the whole body here.
        res = c.post("/api/admin/users", headers={"X-Admin-Key": KEY},
                     json={"whatsapp_number": "+910000000000", "angel_password": S_PIN,
                           "angel_totp_secret": S_TOTP})
        assert res.status_code == 422, res.text
        assert S_PIN not in res.text and S_TOTP not in res.text
        detail = res.json()["detail"]
        assert all(set(d) == {"loc", "type", "msg"} for d in detail), detail


def test_custom_validator_messages_are_replaced():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel, field_validator

    import web.app as webapp
    from fastapi.exceptions import RequestValidationError

    class Body(BaseModel):
        secret: str

        @field_validator("secret")
        @classmethod
        def reject(cls, v):
            raise ValueError(f"bad value {v}")  # a validator that leaks the value

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, webapp._sanitized_validation_error)

    @app.post("/x")
    def x(body: Body):  # pragma: no cover - never reached
        return {}

    res = TestClient(app).post("/x", json={"secret": S_PIN})
    assert res.status_code == 422
    assert S_PIN not in res.text and "bad value" not in res.text, res.text


def test_conflict_message_is_neutral():
    _reset_guard()
    with _env(), harness() as h:
        c = _client(h.app)
        res = c.post("/api/admin/users", headers={"X-Admin-Key": KEY}, json={
            "whatsapp_number": "+910000000099", "angel_api_key": "x", "angel_client_id": USERS["A"]["client_id"],
            "angel_password": S_PIN, "angel_totp_secret": S_TOTP})
        assert res.status_code == 409, res.text
        assert USERS["A"]["client_id"] not in res.text and "id=" not in res.text


def test_only_loopback_without_forwarding_headers_is_allowed():
    _reset_guard()
    with _env(), harness() as h:
        assert _client(h.app, ("testclient", 1)).get(
            "/api/admin/users", headers={"X-Admin-Key": KEY}).status_code == 404
        assert _client(h.app, ("203.0.113.5", 1)).get(
            "/api/admin/users", headers={"X-Admin-Key": KEY}).status_code == 404
        # Public traffic always arrives through the proxy, with forwarding headers.
        assert _client(h.app).get("/api/admin/users", headers={
            "X-Admin-Key": KEY, "X-Forwarded-For": "127.0.0.1"}).status_code == 404
        assert _client(h.app).get("/api/admin/users", headers={"X-Admin-Key": KEY}).status_code == 200


def test_remote_needs_an_allowlist_and_a_verified_ip():
    _reset_guard()
    allow = {"ADMIN_ALLOW_REMOTE": "1", "ADMIN_ALLOWED_SOURCES": "198.51.100.0/24"}
    with harness() as h:
        remote = _client(h.app, ("10.1.2.3", 1))
        with _env(ADMIN_ALLOW_REMOTE="1"):
            assert remote.get("/api/admin/users", headers={
                "X-Admin-Key": KEY, "X-Forwarded-For": "198.51.100.7"}).status_code == 404
        with _env(CLIENT_IP_MODE="shadow", **allow):
            assert remote.get("/api/admin/users", headers={
                "X-Admin-Key": KEY, "X-Forwarded-For": "198.51.100.7"}).status_code == 404, \
                "shadow mode cannot verify the address"
        with _env(CLIENT_IP_MODE="render_last", **allow):
            # Forged leftmost entry inside the allowlist; the proxy-written one is not.
            assert remote.get("/api/admin/users", headers={
                "X-Admin-Key": KEY, "X-Forwarded-For": "198.51.100.7, 203.0.113.9"}).status_code == 404
            assert remote.get("/api/admin/users", headers={
                "X-Admin-Key": KEY, "X-Forwarded-For": "203.0.113.9, 198.51.100.7"}).status_code == 200


def test_short_or_missing_key_is_503():
    _reset_guard()
    with harness() as h:
        c = _client(h.app)
        with _env(ADMIN_API_KEY="short-key"):
            assert c.get("/api/admin/users", headers={"X-Admin-Key": "short-key"}).status_code == 503
        with _env(ADMIN_API_KEY=""):
            assert c.get("/api/admin/users", headers={"X-Admin-Key": "x"}).status_code == 503


def test_production_does_not_mount_without_the_flag():
    from web.app import create_app

    prod = {
        "APP_ENV": "production", "ALLOWED_HOSTS": "testserver", "SESSION_SECRET": "s" * 40,
        "ENCRYPTION_KEY": "x", "PUBLIC_BASE_URL": "https://testserver", "ADMIN_API_KEY": KEY,
    }
    # Without a key header a mounted admin route answers 422 (header missing);
    # an unmounted one does not exist at all: 404.
    with mock.patch.dict(os.environ, prod):
        os.environ.pop("ADMIN_API_ENABLED", None)
        assert _client(create_app()).get("/api/admin/users").status_code == 404
        with mock.patch.dict(os.environ, {"ADMIN_API_ENABLED": "1"}):
            assert _client(create_app()).get("/api/admin/users").status_code == 422


def test_wrong_keys_never_lock_out_the_right_one():
    _reset_guard()
    with _env(), harness() as h, mock.patch("adminApi.guard.FAILURE_DELAY_SEC", 0.0):
        c = _client(h.app)
        codes = [c.get("/api/admin/users", headers={"X-Admin-Key": "wrong"}).status_code
                 for _ in range(15)]
        assert codes[:10] == [401] * 10 and set(codes[10:]) == {429}, codes
        started = time.monotonic()
        assert c.get("/api/admin/users", headers={"X-Admin-Key": KEY}).status_code == 200
        assert time.monotonic() - started < 1.0


def test_global_failure_alert_is_logged_once():
    import logging

    from adminApi.guard import GLOBAL_ALERT_PER_HOUR, AdminGuard

    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    h = H()
    logging.getLogger("adminApi.guard").addHandler(h)
    try:
        g = AdminGuard(clock=lambda: 1000.0)
        delays = [g.record_failure(f"src{i}") for i in range(GLOBAL_ALERT_PER_HOUR + 20)]
    finally:
        logging.getLogger("adminApi.guard").removeHandler(h)
    assert sum("ADMIN_AUTH_FAILURES_HIGH" in m for m in seen) == 1, seen
    assert delays[0] == 0.0 and delays[-1] > 0


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
