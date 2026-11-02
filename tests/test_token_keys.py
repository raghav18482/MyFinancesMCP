"""Data keys and token keys (db/crypto.py), and the two token consumers over HTTP.

Covers: kid lookup, audiences both ways, TTL per key, rotation (t1 works
after t2 is added, fails after t1 is removed), exact-version data decryption,
production key checks, the refusal of pre-R8 tokens (no kid, sealed with
``ENCRYPTION_KEY``) since R9, and the feedback (React and Jinja) and OAuth
consent flows end to end.

Run directly: ``python tests/test_token_keys.py``.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from unittest import mock

from cryptography.fernet import Fernet, InvalidToken

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from db import crypto  # noqa: E402

K1 = Fernet.generate_key().decode()
K2 = Fernet.generate_key().decode()
DATA1 = Fernet.generate_key().decode()
DATA2 = Fernet.generate_key().decode()


@contextmanager
def keys(**env):
    base = {"ENCRYPTION_KEY": DATA1, "TOKEN_KEYS": f"t1:{K1}"}
    base.update(env)
    names = ("TOKEN_KEYS", "TOKEN_PRIMARY", "DATA_ENCRYPTION_KEYS", "DATA_ENCRYPTION_PRIMARY", "ENCRYPTION_KEY")
    with mock.patch.dict(os.environ, {}):
        for n in names:
            os.environ.pop(n, None)
        os.environ.update({k: v for k, v in base.items() if v is not None})
        yield


def old_format(payload: dict) -> str:
    """A token as releases before R8 sealed it: no kid, ENCRYPTION_KEY."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return Fernet(os.environ["ENCRYPTION_KEY"].encode()).encrypt(raw).decode()


def _bad(fn):
    try:
        fn()
    except crypto.TokenError:
        return
    raise AssertionError("expected TokenError")


# ── tokens ────────────────────────────────────────────────────────────────


def test_round_trip_with_kid_and_audience():
    with keys():
        tok = crypto.seal_token({"n": 1}, audience="feedback")
        assert tok.startswith("t1.")
        assert crypto.open_token(tok, audience="feedback", max_age=60) == {"n": 1, "aud": "feedback"}


def test_audience_is_checked_both_ways():
    with keys():
        fb = crypto.seal_token({"n": 1}, audience="feedback")
        oa = crypto.seal_token({"v": 1}, audience="oauth_pending")
        _bad(lambda: crypto.open_token(fb, audience="oauth_pending", max_age=60))
        _bad(lambda: crypto.open_token(oa, audience="feedback", max_age=60))
        try:
            crypto.seal_token({"aud": "feedback"}, audience="oauth_pending")
        except ValueError:
            pass
        else:
            raise AssertionError("a payload may not choose its own audience")


def test_unknown_kid_tampering_and_garbage_fail():
    with keys():
        tok = crypto.seal_token({"n": 1}, audience="feedback")
        _bad(lambda: crypto.open_token("t9." + tok.split(".", 1)[1], audience="feedback", max_age=60))
        _bad(lambda: crypto.open_token(tok[:-4] + "AAAA", audience="feedback", max_age=60))
        for junk in ("", ".", "t1.", "t1.not-a-token", "é.x", None):
            _bad(lambda: crypto.open_token(junk, audience="feedback", max_age=60))


def test_ttl_is_enforced():
    with keys():
        with mock.patch("time.time", return_value=time.time() - 120):
            old = crypto.seal_token({"n": 1}, audience="feedback")
        _bad(lambda: crypto.open_token(old, audience="feedback", max_age=60))
        assert crypto.open_token(old, audience="feedback", max_age=600)["n"] == 1


def test_rotation_keeps_old_tokens_until_their_key_is_removed():
    with keys(TOKEN_KEYS=f"t1:{K1}"):
        old = crypto.seal_token({"n": 1}, audience="feedback")
    with keys(TOKEN_KEYS=f"t1:{K1},t2:{K2}", TOKEN_PRIMARY="t2"):
        new = crypto.seal_token({"n": 2}, audience="feedback")
        assert new.startswith("t2.")
        assert crypto.open_token(old, audience="feedback", max_age=60)["n"] == 1
    with keys(TOKEN_KEYS=f"t2:{K2}"):
        assert crypto.open_token(new, audience="feedback", max_age=60)["n"] == 2
        _bad(lambda: crypto.open_token(old, audience="feedback", max_age=60))


def test_token_keys_are_not_data_keys():
    with keys():
        tok = crypto.seal_token({"n": 1}, audience="feedback")
        try:
            Fernet(DATA1.encode()).decrypt(tok.split(".", 1)[1].encode())
        except InvalidToken:
            pass
        else:
            raise AssertionError("a token opened with the data key")


def test_key_config_errors():
    for env in ({"TOKEN_KEYS": f"t1:{K1},t2:{K2}"},            # two keys, no primary
                {"TOKEN_KEYS": f"t1:{K1}", "TOKEN_PRIMARY": "t2"},
                {"TOKEN_KEYS": "T1:" + K1},                      # kid must be lower case
                {"TOKEN_KEYS": "t1:not-a-key"},
                {"TOKEN_KEYS": f"t1:{K1},t1:{K2}"}):
        with keys(**env):
            try:
                crypto.seal_token({}, audience="feedback")
            except crypto.KeyConfigError:
                pass
            else:
                raise AssertionError(env)


def test_production_key_checks():
    with keys(TOKEN_KEYS=None), mock.patch.dict(os.environ, {"APP_ENV": "production"}):
        for env, needle in (({}, "TOKEN_KEYS must be set"),
                            ({"TOKEN_KEYS": f"t1:{DATA1}"}, "must not also be a data key"),
                            ({"TOKEN_KEYS": f"t1:{K1},t2:{K1}", "TOKEN_PRIMARY": "t1"}, "same key twice"),
                            ({"TOKEN_KEYS": f"t1:{K1}", "DATA_ENCRYPTION_KEYS": f"1:{DATA2}"}, "versions 2 and up")):
            with mock.patch.dict(os.environ, env):
                try:
                    crypto.validate_key_config(production=True)
                except crypto.KeyConfigError as e:
                    assert needle in str(e), (needle, str(e))
                    assert K1 not in str(e) and DATA1 not in str(e)
                else:
                    raise AssertionError(env)
        with mock.patch.dict(os.environ, {"TOKEN_KEYS": f"t1:{K1}"}):
            crypto.validate_key_config(production=True)


def test_development_without_token_keys_uses_a_process_key():
    with keys(TOKEN_KEYS=None):
        os.environ.pop("APP_ENV", None)
        os.environ.pop("RENDER", None)
        tok = crypto.seal_token({"n": 1}, audience="feedback")
        assert tok.startswith("dev.")
        assert crypto.open_token(tok, audience="feedback", max_age=60)["n"] == 1


# ── data keys ─────────────────────────────────────────────────────────────


def test_data_decryption_uses_exactly_the_stored_version():
    with keys(DATA_ENCRYPTION_KEYS=f"2:{DATA2}", DATA_ENCRYPTION_PRIMARY="2"):
        legacy = crypto.encrypt_value("8364")
        assert crypto.decrypt_value(legacy) == "8364", "ENCRYPTION_KEY alone still decrypts v1"
        ct, version = crypto.encrypt_value_versioned("8364")
        assert version == 2
        assert crypto.decrypt_value(ct, version=2) == "8364"
        for wrong in ((ct, 1), (legacy, 2), (ct, 7)):
            try:
                crypto.decrypt_value(*wrong)
            except InvalidToken:
                pass
            else:
                raise AssertionError(f"decrypted with the wrong version: {wrong[1]}")


# ── pre-R8 tokens: refused since R9 ──────────────────────────────────────


def test_old_format_tokens_are_refused():
    with keys():
        for payload, audience in (({"purpose": "feedback", "nonce": "n1"}, "feedback"),
                                  ({"v": 1}, "oauth_pending")):
            _bad(lambda: crypto.open_token(old_format(payload), audience=audience, max_age=60))
        assert not hasattr(crypto, "sign_token") and not hasattr(crypto, "unsign_token")


# ── HTTP: feedback ────────────────────────────────────────────────────────


@contextmanager
def feedback_app():
    from test_feedback_relay import app

    with keys(), app() as (client, mocks, engine):
        yield client


def _post_feedback(client, token):
    return client.post("/api/feedback", json={"token": token, "feedback": "Nice", "channel": "react"})


def test_feedback_react_flow_and_replay():
    with feedback_app() as client:
        token = client.get("/api/feedback/token").json()["token"]
        assert token.startswith("t1.")
        assert _post_feedback(client, token).status_code == 200
        assert _post_feedback(client, token).status_code == 403, "replay"


def test_feedback_jinja_flow():
    with feedback_app() as client:
        html = client.get("/").text
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        assert token.startswith("t1.")
        assert _post_feedback(client, token).status_code == 200


def test_feedback_rejects_an_oauth_token_and_an_old_format_token():
    with feedback_app() as client:
        oauth_tok = crypto.seal_token({"v": 1, "nonce": "x"}, audience="oauth_pending")
        assert _post_feedback(client, oauth_tok).status_code == 403
        old = old_format({"purpose": "feedback", "nonce": "old-1"})
        assert _post_feedback(client, old).status_code == 403


# ── HTTP: OAuth consent ───────────────────────────────────────────────────


def test_oauth_consent_flow_and_cross_audience():
    from test_mcp_auth import mcp_harness
    from test_mcp_oauth import _authorize, _blob, _client, _code, _consent, _exchange, _pkce

    with keys(), mcp_harness() as h:
        client = _client(h.http)
        verifier, challenge = _pkce()
        blob = _blob(_authorize(h.http, client, challenge))
        assert blob.startswith("t1.")
        assert h.http.get("/oauth/consent", params={"request": blob}).status_code == 200
        code = _code(_consent(h.http, blob, "A"))
        assert _exchange(h.http, client, code, verifier).status_code == 200

        fb = crypto.seal_token({"nonce": "n"}, audience="feedback")
        assert h.http.get("/oauth/consent", params={"request": fb}).status_code == 400

        # A consent page opened before R8: its blob is in the old format.
        request = crypto.open_token(blob, audience="oauth_pending", max_age=600)
        request.pop("aud")
        assert h.http.get("/oauth/consent", params={"request": old_format(request)}).status_code == 400


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
