"""Client IP trust (web/client_ip.py), its use in throttles, and /__diag/ip.

Run directly: ``python tests/test_client_ip.py``.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import client_ip as cip  # noqa: E402

FORGED = "6.6.6.6"
VISITOR = "203.0.113.7"
PROXY = "198.51.100.9"


def _h(xff=None, **extra):
    h = {}
    if xff is not None:
        h["x-forwarded-for"] = xff
    h.update(extra)
    return h


def _mode(mode, **env):
    return mock.patch.dict(os.environ, {"CLIENT_IP_MODE": mode, **env})


def test_shadow_trusts_nothing():
    with _mode("shadow"):
        assert cip.trusted_client_ip(_h(f"{FORGED}, {VISITOR}"), PROXY) is None
        assert cip.trusted_client_ip(_h(), PROXY) is None
    with mock.patch.dict(os.environ, {}, clear=True):
        assert cip.trusted_client_ip(_h(VISITOR), PROXY) is None, "unset means shadow"


def test_unknown_mode_falls_back_to_shadow():
    with _mode("trust_everything"):
        assert cip.trusted_client_ip(_h(VISITOR), PROXY) is None


def test_render_last_ignores_a_forged_leftmost_entry():
    with _mode("render_last"):
        assert cip.trusted_client_ip(_h(f"{FORGED}, {VISITOR}"), PROXY) == VISITOR
        assert cip.trusted_client_ip(_h(VISITOR), PROXY) == VISITOR
        assert cip.trusted_client_ip(_h(""), PROXY) is None


def test_render_first():
    with _mode("render_first"):
        assert cip.trusted_client_ip(_h(f"{VISITOR}, {PROXY}"), None) == VISITOR


def test_edge_secret_needs_the_exact_secret():
    with _mode("edge_secret", EDGE_AUTH_SECRET="s3cret-value"):
        chain = f"{VISITOR}, {PROXY}"
        assert cip.trusted_client_ip(_h(chain, **{"x-edge-auth": "s3cret-value"})) == VISITOR
        assert cip.trusted_client_ip(_h(chain, **{"x-edge-auth": "wrong"})) == PROXY
        assert cip.trusted_client_ip(_h(chain)) == PROXY
    with _mode("edge_secret", EDGE_AUTH_SECRET=""):
        assert cip.trusted_client_ip(_h(f"{VISITOR}, {PROXY}", **{"x-edge-auth": ""})) == PROXY


def test_abuse_ip_is_best_effort_leftmost():
    assert cip.abuse_client_ip(_h(f"{FORGED}, {VISITOR}"), PROXY) == FORGED
    assert cip.abuse_client_ip(_h(), PROXY) == PROXY
    assert cip.abuse_client_ip(_h(), None) == "unknown"


def test_throttle_without_a_trusted_ip_still_caps_the_identifier():
    from services import login_throttle as lt

    lt._by_identifier.clear()
    lt._by_ip.clear()
    for _ in range(lt._MAX_PER_IDENTIFIER):
        lt.check("+910000000000", None)
    try:
        lt.check("+910000000000", None)
    except lt.TooManyAttempts:
        pass
    else:
        raise AssertionError("identifier cap must apply when the IP is unknown")
    assert not lt._by_ip, "an unknown IP must not create a per-IP entry"
    lt.clear("+910000000000", None)


def test_forged_ips_cannot_reset_the_identifier_cap():
    from services import login_throttle as lt

    lt._by_identifier.clear()
    lt._by_ip.clear()
    for i in range(lt._MAX_PER_IDENTIFIER):
        lt.check("+910000000001", f"10.0.0.{i}")
    try:
        lt.check("+910000000001", "10.0.9.9")
    except lt.TooManyAttempts:
        pass
    else:
        raise AssertionError("a fresh IP must not bypass the identifier cap")


def test_feedback_rate_map_is_bounded():
    from services import feedback

    feedback._rate_hits.clear()
    for i in range(feedback._MAX_RATE_KEYS + 500):
        feedback.check_rate_limit(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
    assert len(feedback._rate_hits) <= feedback._MAX_RATE_KEYS
    feedback._rate_hits.clear()


def test_oauth_registration_map_is_bounded():
    from services import oauth_provider

    oauth_provider.reset_rate_limits()
    for i in range(oauth_provider._MAX_REGISTER_KEYS + 500):
        oauth_provider.check_registration_rate(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
    assert len(oauth_provider._register_by_ip) <= oauth_provider._MAX_REGISTER_KEYS
    oauth_provider.reset_rate_limits()


def _diag(env, k="tok", headers=None):
    from fastapi.testclient import TestClient

    from web.app import create_app

    with mock.patch.dict(os.environ, env):
        for key in ("IP_DIAG_TOKEN", "IP_DIAG_UNTIL"):
            if key not in env:
                os.environ.pop(key, None)
        with TestClient(create_app()) as client:
            return client.get("/__diag/ip", params={"k": k}, headers=headers or {})


def test_diag_is_off_by_default_expired_or_too_far_ahead():
    now = datetime.now(timezone.utc)
    assert _diag({}).status_code == 404
    assert _diag({"IP_DIAG_TOKEN": "tok"}).status_code == 404
    past = (now - timedelta(minutes=1)).isoformat()
    assert _diag({"IP_DIAG_TOKEN": "tok", "IP_DIAG_UNTIL": past}).status_code == 404
    far = (now + timedelta(hours=3)).isoformat()
    assert _diag({"IP_DIAG_TOKEN": "tok", "IP_DIAG_UNTIL": far}).status_code == 404
    naive = (now + timedelta(minutes=30)).replace(tzinfo=None).isoformat()
    assert _diag({"IP_DIAG_TOKEN": "tok", "IP_DIAG_UNTIL": naive}).status_code == 404


def test_diag_needs_the_token_and_reports_the_chain():
    until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    env = {"IP_DIAG_TOKEN": "tok", "IP_DIAG_UNTIL": until}
    assert _diag(env, k="nope").status_code == 404
    res = _diag(env, headers={"X-Forwarded-For": f"{FORGED}, {VISITOR}"})
    assert res.status_code == 200, res.text
    assert res.json()["x_forwarded_for"] == [FORGED, VISITOR]
    assert res.headers["cache-control"] == "no-store"


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
