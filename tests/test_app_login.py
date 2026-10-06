"""Tests for the stored-credential login: hashing, throttling, and the
WhatsApp-number + app-password path that replaced the four-field form.

Run directly (``python tests/test_app_login.py``) or under pytest.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet

from db.passwords import (
    MIN_PASSWORD_CHARS,
    hash_password,
    needs_rehash,
    verify_password,
)

GOOD_PASSWORD = "correct-horse-battery"
WRONG_PASSWORD = "incorrect-horse-batter"


# ── Hashing ────────────────────────────────────────────────────────────────


def test_hash_round_trips():
    stored = hash_password(GOOD_PASSWORD)
    assert verify_password(GOOD_PASSWORD, stored)
    print("ok  the right password verifies")


def test_wrong_password_is_rejected():
    stored = hash_password(GOOD_PASSWORD)
    assert not verify_password(WRONG_PASSWORD, stored)
    assert not verify_password("", stored)
    print("ok  a wrong or empty password is rejected")


def test_hash_is_salted():
    """Two users with the same password must not share a hash, or one cracked
    hash would crack every account using that password."""
    a = hash_password(GOOD_PASSWORD)
    b = hash_password(GOOD_PASSWORD)
    assert a != b, "identical passwords produced identical hashes — salt missing"
    assert verify_password(GOOD_PASSWORD, a) and verify_password(GOOD_PASSWORD, b)
    print("ok  equal passwords hash differently")


def test_password_is_never_recoverable_from_the_hash():
    """The stored form must not contain the password in any readable encoding."""
    import base64

    stored = hash_password(GOOD_PASSWORD)
    assert GOOD_PASSWORD not in stored
    raw = GOOD_PASSWORD.encode()
    for encoded in (base64.b64encode(raw), base64.b16encode(raw), base64.b32encode(raw)):
        assert encoded.decode() not in stored
    print("ok  the hash leaks no encoding of the password")


def test_short_passwords_are_refused():
    try:
        hash_password("x" * (MIN_PASSWORD_CHARS - 1))
    except ValueError:
        print("ok  a too-short password is refused at hashing time")
    else:
        raise AssertionError("short password was accepted")


def test_missing_or_malformed_hash_fails_closed():
    """A user with no app password, or a corrupted row, must not log in — and
    must not raise, since both reach the live login path."""
    assert not verify_password(GOOD_PASSWORD, None)
    assert not verify_password(GOOD_PASSWORD, "")
    assert not verify_password(GOOD_PASSWORD, "not-a-hash")
    assert not verify_password(GOOD_PASSWORD, "scrypt$bad$8$1$xx$yy")
    assert not verify_password(GOOD_PASSWORD, "md5$1$1$1$aaaa$bbbb")
    print("ok  absent and malformed hashes fail closed")


def test_needs_rehash_tracks_the_cost_parameters():
    stored = hash_password(GOOD_PASSWORD)
    assert not needs_rehash(stored), "a freshly made hash should not need upgrading"

    scheme, n, r, p, salt, key = stored.split("$")
    weaker = f"{scheme}${int(n) // 2}${r}${p}${salt}${key}"
    assert needs_rehash(weaker), "a lower cost factor should be flagged"
    assert not needs_rehash(None)
    print("ok  needs_rehash flags outdated parameters only")


# ── The phone field ────────────────────────────────────────────────────────


def test_country_dropdown_defaults_to_india():
    from web import country_codes

    opts = country_codes.options()
    assert opts[0]["dial"] == country_codes.DEFAULT_DIAL_CODE == "91"
    assert opts[0]["name"] == "India"
    names = [o["name"] for o in opts[1:]]
    assert names == sorted(names), "countries after India should be alphabetical"
    print("ok  the dropdown lists India first, then alphabetically")


def test_formatting_differences_produce_one_identical_number():
    """The whole reason this normaliser is shared. If ``/enroll`` and ``/login``
    disagreed by a single space, the lookup would miss and the user would be
    told their password was wrong."""
    from web.country_codes import normalize_whatsapp

    typed = ["9876543210", "98765 43210", "98765-43210", " 98765 43210 ",
             "(98765) 43210", "09876543210"]
    results = {normalize_whatsapp("91", t) for t in typed}
    assert results == {"+919876543210"}, results
    print("ok  spacing, punctuation and a leading zero all normalise the same")


def test_dial_code_is_accepted_with_or_without_a_plus():
    from web.country_codes import normalize_whatsapp

    assert normalize_whatsapp("+91", "9876543210") == "+919876543210"
    assert normalize_whatsapp("44", "07700900123") == "+447700900123"
    print("ok  the dial code normalises with or without a +")


def test_unknown_or_malformed_numbers_are_refused():
    from web.country_codes import normalize_whatsapp

    for dial, national in [
        ("999", "9876543210"),   # not a country in the list
        ("", "9876543210"),      # nothing selected
        ("91", ""),              # no number
        ("91", "abcd"),          # no digits at all
        ("91", "000"),           # only trunk zeros
        ("91", "12"),            # too short
        ("91", "1" * 20),        # beyond E.164's 15 digits
    ]:
        try:
            normalize_whatsapp(dial, national)
        except ValueError:
            continue
        raise AssertionError(f"accepted bad input: {dial!r} {national!r}")
    print("ok  unknown codes and malformed numbers are refused")


def test_split_round_trips_for_the_error_redisplay():
    """A rejected form has to show the user what they typed, including the
    longer codes — ``+971`` must not be read as ``+97``."""
    from web.country_codes import normalize_whatsapp, split_whatsapp

    for dial, national in [("91", "9876543210"), ("971", "501234567"),
                           ("1", "4155550123"), ("44", "7700900123")]:
        stored = normalize_whatsapp(dial, national)
        assert split_whatsapp(stored) == (dial, national), stored
    print("ok  a stored number splits back into the same code and number")


# ── Throttling ─────────────────────────────────────────────────────────────


def _fresh_throttle():
    """A throttle module with empty counters, independent of other tests."""
    from services import login_throttle

    login_throttle._by_identifier.clear()
    login_throttle._by_ip.clear()
    return login_throttle


def test_throttle_blocks_after_the_identifier_cap():
    throttle = _fresh_throttle()
    cap = throttle._MAX_PER_IDENTIFIER

    for _ in range(cap):
        throttle.check("+910000000001", "1.2.3.4")

    try:
        throttle.check("+910000000001", "1.2.3.4")
    except throttle.TooManyAttempts as e:
        assert e.retry_after_sec > 0
        print(f"ok  one number is throttled after {cap} attempts")
    else:
        raise AssertionError("throttle never fired")


def test_throttle_blocks_a_sweep_across_many_numbers():
    """The per-identifier cap alone would miss someone trying one guess against
    each of a hundred different numbers from the same host."""
    throttle = _fresh_throttle()
    ip = "9.9.9.9"

    fired_at = None
    for i in range(throttle._MAX_PER_IP + 5):
        try:
            throttle.check(f"+9100000{i:05d}", ip)
        except throttle.TooManyAttempts:
            fired_at = i
            break

    assert fired_at == throttle._MAX_PER_IP, (
        f"per-IP cap fired at {fired_at}, expected {throttle._MAX_PER_IP}"
    )
    print("ok  a sweep across many numbers is throttled per IP")


def test_successful_login_clears_the_counter():
    """Fumbling the password a few times then getting it right must not leave
    the user locked out."""
    throttle = _fresh_throttle()
    number, ip = "+910000000002", "5.6.7.8"

    for _ in range(throttle._MAX_PER_IDENTIFIER - 1):
        throttle.check(number, ip)
    throttle.clear(number, ip)

    for _ in range(throttle._MAX_PER_IDENTIFIER):
        throttle.check(number, ip)
    print("ok  a successful login resets the attempt counter")


# ── The login path, against a real database ────────────────────────────────


def _temp_db():
    """A sqlite database with the tables created, and ENCRYPTION_KEY present."""
    from sqlmodel import SQLModel, Session, create_engine

    from db import models  # noqa: F401 — registers the tables

    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
    tmp = tempfile.TemporaryDirectory()
    engine = create_engine(f"sqlite:///{os.path.join(tmp.name, 't.db')}")
    SQLModel.metadata.create_all(engine)
    return tmp, (lambda: Session(engine))


def _with_service(fn):
    """Run ``fn(service)`` with ``adminApi.service`` bound to a temp database.

    The module does ``from db import get_session``, so the name has to be
    patched on the service module itself, not on ``db.engine``.
    """
    from adminApi import service

    tmp, factory = _temp_db()
    original = service.get_session
    service.get_session = factory
    try:
        return fn(service)
    finally:
        service.get_session = original
        tmp.cleanup()


def _enrol(service, **overrides):
    kwargs = dict(
        whatsapp_number="+919876543210",
        angel_api_key="APIKEY123",
        angel_client_id="AB1234",
        angel_password="4321",
        angel_totp_secret="JBSWY3DPEHPK3PXP",
        app_password=GOOD_PASSWORD,
    )
    kwargs.update(overrides)
    return service.create_user(**kwargs)


def test_login_returns_the_decrypted_credentials():
    def check(service):
        view = _enrol(service)
        creds = service.credentials_for_login("+919876543210", GOOD_PASSWORD)

        assert creds is not None, "the right password did not log in"
        assert creds.user_id == view["id"]
        assert creds.api_key == "APIKEY123"
        assert creds.client_id == "AB1234"
        assert creds.password == "4321", "the PIN must come back decrypted"
        assert creds.totp_secret == "JBSWY3DPEHPK3PXP"
        print("ok  login returns the four credentials, decrypted")

    _with_service(check)


def test_stored_row_holds_no_plaintext_secrets():
    """The point of the feature is that a database dump is not a credential
    dump in plaintext."""
    def check(service):
        from sqlmodel import select

        from db.models import User

        _enrol(service)
        with service.get_session() as s:
            row = s.exec(select(User)).first()

        assert row.angel_password_encrypted != "4321"
        assert row.angel_totp_secret_encrypted != "JBSWY3DPEHPK3PXP"
        assert GOOD_PASSWORD not in (row.app_password_hash or "")
        assert row.app_password_hash.startswith("scrypt$")
        print("ok  the stored row holds no plaintext PIN, TOTP secret or password")

    _with_service(check)


def test_login_is_refused_for_every_kind_of_miss():
    def check(service):
        _enrol(service)

        assert service.credentials_for_login("+919876543210", WRONG_PASSWORD) is None
        assert service.credentials_for_login("+910000000000", GOOD_PASSWORD) is None
        assert service.credentials_for_login("", GOOD_PASSWORD) is None
        assert service.credentials_for_login("+919876543210", "") is None
        print("ok  wrong password, unknown number and blanks are all refused")

    _with_service(check)


def test_premium_row_without_an_app_password_cannot_log_in():
    """Rows created by premium registration predate app passwords. They must
    fail closed rather than letting anyone in with any password."""
    def check(service):
        _enrol(service, app_password=None)
        assert service.credentials_for_login("+919876543210", GOOD_PASSWORD) is None
        assert service.credentials_for_login("+919876543210", "") is None
        print("ok  a row with no app password cannot be logged into")

    _with_service(check)


def test_re_enrolling_sets_a_password_on_an_existing_row():
    """How a premium user claims their row: ``/enroll`` proves the Angel
    credentials, then patches an app password in."""
    def check(service):
        view = _enrol(service, app_password=None)
        assert service.credentials_for_login("+919876543210", GOOD_PASSWORD) is None

        service.update_user(view["id"], app_password=GOOD_PASSWORD)
        creds = service.credentials_for_login("+919876543210", GOOD_PASSWORD)
        assert creds is not None and creds.user_id == view["id"]
        print("ok  re-enrolling sets a password on an existing row")

    _with_service(check)


def test_deactivated_user_cannot_log_in():
    def check(service):
        view = _enrol(service)
        service.deactivate_user(view["id"])

        assert service.credentials_for_login("+919876543210", GOOD_PASSWORD) is None
        assert service.credentials_for_user(view["id"]) is None
        print("ok  a deactivated user cannot log in or be rebuilt from a cookie")

    _with_service(check)


def test_cookie_rebuild_path_returns_credentials_without_a_password():
    def check(service):
        view = _enrol(service)
        creds = service.credentials_for_user(view["id"])

        assert creds is not None and creds.password == "4321"
        assert service.credentials_for_user(view["id"] + 999) is None
        print("ok  the cookie rebuild path works by row id and fails on a bad id")

    _with_service(check)


def test_public_view_never_exposes_the_password_hash():
    """``public_view`` is returned straight to HTTP clients by the admin API."""
    def check(service):
        view = _enrol(service)
        assert "app_password_hash" not in view
        assert "angel_password_encrypted" not in view
        assert "angel_totp_secret_encrypted" not in view
        print("ok  public_view still exposes no secret fields")

    _with_service(check)


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
    print(f"\n{passed} tests passed")
