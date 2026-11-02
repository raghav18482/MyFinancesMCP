"""Client IP trust (web/client_ip.py), its startup checks, and its use in throttles.

Run directly: ``python tests/test_client_ip.py``.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

from cryptography.fernet import Fernet

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


def test_the_chosen_entry_must_be_an_ip_address():
    with _mode("render_last"):
        for junk in ("unknown", "203.0.113.7:443", "<script>", "203.0.113.999", "_hidden"):
            assert cip.trusted_client_ip(_h(f"{VISITOR}, {junk}"), PROXY) is None, junk
        assert cip.trusted_client_ip(_h("2001:DB8::1"), PROXY) == "2001:db8::1"
    with _mode("render_first"):
        assert cip.trusted_client_ip(_h(f"unknown, {VISITOR}"), PROXY) is None
    with _mode("edge_secret", EDGE_AUTH_SECRET="e" * 40):
        assert cip.trusted_client_ip(_h(f"junk, {PROXY}", **{"x-edge-auth": "e" * 40})) is None


def test_production_refuses_a_mistyped_mode_or_a_weak_edge_secret():
    for env in ({"CLIENT_IP_MODE": "render-last"},
                {"CLIENT_IP_MODE": "edge_secret"},
                {"CLIENT_IP_MODE": "edge_secret", "EDGE_AUTH_SECRET": "x" * 31}):
        with mock.patch.dict(os.environ, env):
            try:
                cip.validate_config(production=True)
            except ValueError as e:
                assert "x" * 31 not in str(e)
            else:
                raise AssertionError(f"production accepted {env}")
            cip.validate_config(production=False)  # outside production: log and use shadow
    for env in ({}, {"CLIENT_IP_MODE": "shadow"}, {"CLIENT_IP_MODE": " Render_Last "},
                {"CLIENT_IP_MODE": "edge_secret", "EDGE_AUTH_SECRET": "x" * 32}):
        with mock.patch.dict(os.environ, env):
            if not env:
                os.environ.pop("CLIENT_IP_MODE", None)
            cip.validate_config(production=True)


def test_production_startup_stops_on_a_bad_client_ip_mode():
    from web import security_config

    env = {"APP_ENV": "production", "SESSION_SECRET": "s" * 40, "ALLOWED_HOSTS": "example.com",
           "PUBLIC_BASE_URL": "https://example.com", "ENCRYPTION_KEY": Fernet.generate_key().decode(),
           "TOKEN_KEYS": "t1:" + Fernet.generate_key().decode(), "CLIENT_IP_MODE": "render_lst"}
    with mock.patch.dict(os.environ, env):
        try:
            security_config.validate_production_config()
        except security_config.InsecureConfiguration as e:
            assert "CLIENT_IP_MODE" in str(e)
        else:
            raise AssertionError("production started with an unknown CLIENT_IP_MODE")
        os.environ["CLIENT_IP_MODE"] = "render_last"
        security_config.validate_production_config()


def _login_sweep(mode, attempts):
    """``attempts`` logins for different numbers from one address, each forging
    a fresh leftmost entry. Returns the status codes."""
    from _security_harness import harness

    codes = []
    with _mode(mode), harness() as h:
        client = h.client()
        for i in range(attempts):
            res = client.post(
                "/api/auth/login",
                json={"country_code": "91", "whatsapp_number": f"90000{i:05d}",
                      "app_password": "wrong-password-123"},
                headers={"X-Forwarded-For": f"10.9.{i // 256}.{i % 256}, {VISITOR}"},
            )
            codes.append(res.status_code)
    return codes


def test_enforced_mode_caps_one_address_however_the_header_is_forged():
    from services import login_throttle as lt

    codes = _login_sweep("render_last", lt._MAX_PER_IP + 1)
    assert 429 not in codes[:-1], codes
    assert codes[-1] == 429, codes


def test_shadow_mode_never_limits_by_address():
    from services import login_throttle as lt

    codes = _login_sweep("shadow", lt._MAX_PER_IP + 5)
    assert 429 not in codes, codes


def test_throttle_sweeps_only_expired_keys():
    from services import login_throttle as lt

    lt._by_identifier.clear()
    lt._by_ip.clear()
    with mock.patch.object(lt, "_PRUNE_ABOVE", 50):
        old = lt.time.time() - lt._WINDOW_SEC - 1
        for i in range(60):
            lt._by_ip[f"10.0.0.{i}"] = [old]
        lt._by_ip["10.1.1.1"] = [lt.time.time()] * 3
        lt.check("+910000000099", "10.2.2.2")
    assert set(lt._by_ip) == {"10.1.1.1", "10.2.2.2"}, sorted(lt._by_ip)
    assert len(lt._by_ip["10.1.1.1"]) == 3, "a live count must survive the sweep"
    lt._by_identifier.clear()
    lt._by_ip.clear()


def test_the_diagnostic_endpoint_is_gone():
    from fastapi.testclient import TestClient

    from web.app import create_app

    with mock.patch.dict(os.environ, {"IP_DIAG_TOKEN": "tok", "IP_DIAG_UNTIL": "2999-01-01T00:00:00Z"}):
        with TestClient(create_app()) as client:
            assert client.get("/__diag/ip", params={"k": "tok"}).status_code in (404, 405)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not os.path.exists(os.path.join(root, "web", "routers", "ip_diag.py"))


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
