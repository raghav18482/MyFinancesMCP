"""Angel One login retry policy and the enrollment throttle.

A rejected login is never retried (a repeated wrong PIN counts toward Angel's
lockout); only network failures are, and boundedly. Enrollment, which makes a
real login with a typed client id, is capped per client id and globally.

Run directly: ``python tests/test_broker_login.py``.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import angel_client  # noqa: E402
from angel_client import AngelAuthError, AngelOneClient  # noqa: E402

OK = {"status": True, "data": {"jwtToken": "Bearer jwt-value-0001", "refreshToken": "refresh-0001",
                               "feedToken": "feed-0001"}}
REJECTED = {"status": False, "message": "Invalid PIN", "errorcode": "AB1050", "data": None}


def _client():
    return AngelOneClient("APIKEY99", "AB1234", "4321", "JBSWY3DPEHPK3PXPJBSWY3DPEH")


def _login(results):
    """Run a login where generateSession returns/raises each of ``results`` in turn."""
    client = _client()
    calls = mock.Mock(side_effect=results)
    with mock.patch.object(client._smart_api, "generateSession", calls), \
            mock.patch.object(angel_client.time, "sleep") as sleep:
        try:
            client.ensure_session()
            error = None
        except AngelAuthError as e:
            error = e
    client.release_secrets()
    return calls.call_count, [c.args[0] for c in sleep.call_args_list], error, client


def test_a_rejection_is_tried_once():
    calls, sleeps, error, _ = _login([REJECTED, OK])
    assert calls == 1 and sleeps == []
    assert error.kind == "credentials" and error.errorcode == "AB1050"
    assert isinstance(error, RuntimeError), "callers catching RuntimeError still work"


def test_a_smartapi_api_error_is_tried_once():
    from SmartApi.smartExceptions import DataException

    calls, sleeps, error, _ = _login([DataException("bad input"), OK])
    assert calls == 1 and sleeps == [] and error.kind == "credentials"


def test_network_errors_are_retried_with_backoff_then_give_up():
    failures = [requests.exceptions.ConnectionError("down"), requests.exceptions.Timeout("slow"),
                ConnectionError("reset")]
    calls, sleeps, error, _ = _login(failures)
    assert calls == angel_client.LOGIN_ATTEMPTS == 3
    assert sleeps == list(angel_client.LOGIN_BACKOFF_SEC)
    assert error.kind == "unavailable"


def test_a_network_blip_then_success_logs_in():
    calls, sleeps, error, client = _login([requests.exceptions.ConnectionError("blip"), OK])
    assert error is None and calls == 2 and sleeps == [0.5]
    assert client.auth_token == "Bearer jwt-value-0001"


def test_a_network_error_then_a_rejection_stops():
    calls, _sleeps, error, _ = _login([requests.exceptions.Timeout("slow"), REJECTED, OK])
    assert calls == 2 and error.kind == "credentials"


def test_requests_have_a_timeout():
    client = _client()
    try:
        assert client._smart_api.timeout == angel_client.LOGIN_TIMEOUT_SEC == 5
    finally:
        client.release_secrets()


def _enroll(h, client_id):
    from test_spa_api import ENROLL

    return h.http.post("/api/auth/enroll", json={**ENROLL, "client_id": client_id})


def test_enrollment_is_capped_per_client_id():
    from test_spa_api import harness

    with harness() as h:
        h.broker["accepts"] = False
        codes = [_enroll(h, "ZZ0001").status_code for _ in range(6)]
        assert codes[:5] == [400] * 5 and codes[5] == 429, codes
        # Case and spaces do not make a fresh identifier.
        assert _enroll(h, " zz0001 ").status_code == 429


def test_enrollment_is_capped_across_client_ids():
    from test_spa_api import harness

    import web.auth_flow as af

    with harness() as h, mock.patch.object(af, "ENROLL_GLOBAL_CAP", 3):
        h.broker["accepts"] = False
        codes = [_enroll(h, f"YY{i:04d}").status_code for i in range(4)]
        assert codes == [400, 400, 400, 429], codes


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
