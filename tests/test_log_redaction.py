"""No broker secret reaches any log output.

Sentinel values stand in for the Angel One API key, PIN, TOTP code and session
JWT. Each test drives a log path that could carry them — SmartAPI's own request
logging, our handlers, uvicorn's handlers, a JSON formatter, exception chains,
``extra`` fields, objects whose ``repr`` holds a secret, reconfiguration by
other code — and asserts that no sentinel appears in what was written.

Run directly: ``python tests/test_log_redaction.py``.
"""
from __future__ import annotations

import contextlib
import glob
import io
import json
import logging
import logging.config
import os
import sys
import tempfile
import threading
import traceback
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import logging_config as lc  # noqa: E402
from services import secret_registry  # noqa: E402
from services.secret_registry import SecretRegistry  # noqa: E402

S_KEY = "Kx9TqLm2"                 # API key shape: 8 alphanumerics
S_PIN = "4817"                     # numeric PIN
S_TOTP = "582913"                  # a TOTP code
S_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEH"
S_JWT = "eyJhbGciOiJIUzUxMiJ9.eyJ1c2VybmFtZSI6IlNFTlRJTkVMIn0.c2VudGluZWxfc2lnbmF0dXJl"
SENTINELS = (S_KEY, S_PIN, S_TOTP, S_TOTP_SECRET, S_JWT)


def _assert_clean(text: str, where: str) -> None:
    leaked = [s for s in SENTINELS if s in text]
    assert not leaked, f"{where} leaked {leaked}:\n{text}"


class JsonDumpFormatter(logging.Formatter):
    """A formatter that writes every record attribute, as JSON log shippers do."""

    def format(self, record: logging.LogRecord) -> str:
        out = {k: repr(v) for k, v in record.__dict__.items()}
        out["formatted_message"] = record.getMessage()
        if record.exc_info:
            out["exc"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(out)


@contextlib.contextmanager
def captured(logger_name: str = "test.redaction"):
    """A logger with a plain and a JSON handler, both carrying the filter."""
    plain, js = io.StringIO(), io.StringIO()
    h1 = logging.StreamHandler(plain)
    h1.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    h2 = logging.StreamHandler(js)
    h2.setFormatter(JsonDumpFormatter())
    for h in (h1, h2):
        h.addFilter(lc.RedactingFilter())
    lg = logging.getLogger(logger_name)
    lg.handlers = [h1, h2]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    try:
        yield lg, lambda: plain.getvalue() + "\n" + js.getvalue()
    finally:
        lg.handlers = []


@contextlib.contextmanager
def owned_config_streams():
    """Apply the real LOGGING config with stdout/stderr captured."""
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        lc.apply_config(lc.build_config())
        yield lambda: out.getvalue() + "\n" + err.getvalue()
    finally:
        sys.stdout, sys.stderr = saved
        lc.apply_config(lc.build_config())


@contextlib.contextmanager
def registered(*values: str):
    owner = "test-owner"
    secret_registry.register(owner, *values)
    try:
        yield
    finally:
        secret_registry.release(owner)


# ── Patterns ──────────────────────────────────────────────────────────────


def test_smartapi_style_line_is_redacted():
    line = (
        "Error occurred while making a POST request to https://x/loginByPassword. "
        f"Headers: {{'X-PrivateKey': '{S_KEY}', 'Authorization': 'Bearer {S_JWT}'}}, "
        f"Request: {{'clientcode': 'AB1234', 'password': '{S_PIN}', 'totp': '{S_TOTP}'}}"
    )
    out = lc.redact(line)
    _assert_clean(out, "redact()")
    assert "AB1234" in out, "the client code is an identifier and stays visible"


def test_key_names_need_whole_words():
    out = lc.redact(f"pinned=3 spin=4 pin={S_PIN}")
    assert "pinned=3" in out and "spin=4" in out
    _assert_clean(out, "redact()")


def test_fernet_and_query_api_key_are_redacted():
    out = lc.redact(f"key=gAAAAABpQ7xYzWx1234567890abcdef url?api_key={S_KEY}&x=1")
    assert "gAAAAAB" not in out and "&x=1" in out
    _assert_clean(out, "redact()")


# ── The filter, through plain and JSON output ────────────────────────────


def test_message_args_and_extra():
    with captured() as (lg, output):
        lg.error("Request: %s", {"password": S_PIN, "totp": S_TOTP})
        lg.info("pin=%s api_key=%s", S_PIN, S_KEY)
        with registered(S_KEY):  # a live client's key, logged under any name
            lg.info("key=%s", S_KEY)
        lg.info(
            "ctx",
            extra={
                "headers": {"X-PrivateKey": S_KEY},
                "payload": [{"totp": S_TOTP}],
                "note": f"Bearer {S_JWT}",
                "count": 3,
            },
        )
        _assert_clean(output(), "message/args/extra")


def test_extra_keeps_numbers():
    seen = {}

    class Keep(logging.Handler):
        def emit(self, record):
            seen["count"] = record.count

    h = Keep()
    h.addFilter(lc.RedactingFilter())
    lg = logging.getLogger("test.redaction.keep")
    lg.handlers, lg.propagate = [h], False
    lg.info("x", extra={"count": 3})
    lg.handlers = []
    assert seen["count"] == 3 and isinstance(seen["count"], int)


def test_exception_chains_and_groups():
    with captured() as (lg, output):
        try:
            try:
                raise ValueError(f"inner password={S_PIN}")
            except ValueError as inner:
                raise RuntimeError(f"outer Bearer {S_JWT}") from inner
        except RuntimeError:
            lg.exception("explicit chain")
        try:
            try:
                raise KeyError(f"api_key={S_KEY}")
            except KeyError:
                raise RuntimeError("implicit chain")
        except RuntimeError:
            lg.error("context chain", exc_info=True)
        try:
            raise ExceptionGroup("group", [ValueError(f"totp={S_TOTP}"), TypeError("ok")])
        except ExceptionGroup:
            lg.exception("group")
        lg.info("stack", stack_info=True)
        text = output()
        _assert_clean(text, "exception output")
        assert "explicit chain" in text and "RuntimeError" in text


def test_traceback_text_as_message():
    with captured() as (lg, output):
        try:
            raise RuntimeError(f"password: {S_PIN}")
        except RuntimeError:
            lg.error(traceback.format_exc())
        _assert_clean(output(), "format_exc message")


class Creds:
    """An object whose repr and str print a secret with no key name."""

    def __repr__(self) -> str:
        return f"Creds({S_PIN}, {S_KEY})"

    __str__ = __repr__


def test_untagged_secret_in_repr_str_and_exception_args():
    with registered(S_PIN, S_KEY), captured() as (lg, output):
        creds = Creds()
        lg.info("creds=%r", creds)
        lg.info("creds=%s", creds)
        lg.info("ctx", extra={"obj": creds, "nested": {"list": [creds]}})
        try:
            raise ValueError(creds)
        except ValueError:
            lg.exception("raised with creds")
        _assert_clean(output(), "untagged secret")


def test_numeric_secret_does_not_match_inside_longer_numbers():
    with registered(S_PIN):
        assert lc.redact(f"qty 1{S_PIN}0 and {S_PIN}") == f"qty 1{S_PIN}0 and [REDACTED]"


def test_angel_client_repr_has_no_secrets():
    from angel_client import AngelOneClient

    client = AngelOneClient(S_KEY, "AB1234", S_PIN, S_TOTP_SECRET)
    try:
        assert repr(client) == "AngelOneClient(client_id='AB1234')"
        with captured() as (lg, output):
            lg.info("client %r", client)
            lg.info("client %s", vars(client))
            _assert_clean(output(), "client repr")
    finally:
        client.release_secrets()


def test_redaction_failure_suppresses_the_record():
    with captured() as (lg, output), mock.patch.object(lc, "redact", side_effect=RuntimeError("boom")):
        lg.error("password=%s", S_PIN, extra={"x": S_KEY})
    text = output()
    assert lc.SUPPRESSED in text, text
    _assert_clean(text, "suppressed record")


# ── SmartAPI itself ──────────────────────────────────────────────────────


class _Response:
    status_code = 200

    def __init__(self, body: dict):
        self.content = json.dumps(body).encode()


def test_smartapi_request_logging_emits_nothing():
    """The real SmartConnect._request, failing two ways, writes nothing anywhere."""
    from integrations import smartapi

    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            records = []

            class Grab(logging.Handler):
                def emit(self, record):
                    records.append(record)

            grab = Grab()
            for lg in smartapi._logzero_loggers():
                lg.addHandler(grab)
            try:
                with owned_config_streams() as output:
                    smartapi.silence()  # dictConfig above re-enabled it, as uvicorn's would
                    client = smartapi.new_smart_connect(api_key=S_KEY)
                    client.setAccessToken(S_JWT)
                    with mock.patch("requests.request", side_effect=ConnectionError("down")):
                        try:
                            client.generateSession("AB1234", S_PIN, S_TOTP)
                        except ConnectionError:
                            pass
                    with mock.patch(
                        "requests.request",
                        return_value=_Response({"status": False, "message": "Invalid PIN",
                                                "errorcode": "AB1050", "data": None}),
                    ):
                        client.generateSession("AB1234", S_PIN, S_TOTP)
                    text = output()
            finally:
                for lg in smartapi._logzero_loggers():
                    lg.removeHandler(grab)

            assert records == [], [r.getMessage() for r in records]
            _assert_clean(text, "stdout/stderr")
            for path in glob.glob(os.path.join(tmp, "logs", "**", "*.log"), recursive=True):
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
                assert content == "", f"{path} has content"
            assert smartapi.is_silenced()
        finally:
            os.chdir(cwd)


def test_angel_client_logs_its_own_sanitized_failure():
    from angel_client import AngelOneClient

    with owned_config_streams() as output:
        client = AngelOneClient(S_KEY, "AB1234", S_PIN, S_TOTP_SECRET)
        try:
            with mock.patch("angel_client.time.sleep"), mock.patch(
                "requests.request",
                return_value=_Response({"status": False, "message": f"bad pin {S_PIN}",
                                        "errorcode": "AB1050", "data": None}),
            ):
                try:
                    client.ensure_session()
                except RuntimeError:
                    pass
            text = output()
        finally:
            client.release_secrets()
    assert "Angel One login attempt 1/5 failed for AB1234: errorcode=AB1050" in text, text
    _assert_clean(text, "angel_client failure log")


# ── Owned configuration, uvicorn, and repair ─────────────────────────────


def test_owned_config_redacts_root_and_uvicorn():
    with owned_config_streams() as output:
        logging.getLogger("services.anything").error("password=%s", S_PIN)
        logging.getLogger("uvicorn.error").info(
            "Started %s", f"Bearer {S_JWT}", extra={"color_message": "Started %s"}
        )
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", f"/x?api_key={S_KEY}", "1.1", 200
        )
        text = output()
    _assert_clean(text, "owned config")
    assert "GET /x?api_key=[REDACTED] HTTP/1.1" in text, text


def test_uvicorn_reconfiguration_then_verify():
    import uvicorn

    from integrations import smartapi

    async def app(scope, receive, send):  # pragma: no cover - never called
        pass

    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        uvicorn.Config(app, log_config=lc.uvicorn_log_config())  # applies dictConfig
        lc.verify_logging()
        assert smartapi.is_silenced()
        logging.getLogger("uvicorn.error").error("token=%s", S_JWT)
        logging.getLogger().error("pin=%s", S_PIN)
    finally:
        sys.stdout, sys.stderr = saved
        lc.apply_config(lc.build_config())
    _assert_clean(out.getvalue() + err.getvalue(), "after uvicorn config")


def test_verify_repairs_an_unfiltered_handler_and_alerts():
    buf, alerts = io.StringIO(), io.StringIO()
    with owned_config_streams():
        alert = logging.StreamHandler(alerts)
        alert.addFilter(lc.RedactingFilter())
        logging.getLogger(lc.__name__).addHandler(alert)
        stray = logging.StreamHandler(buf)  # added by "a library", no filter
        third = logging.getLogger("thirdparty.lib")
        third.addHandler(stray)
        third.propagate = False
        try:
            repairs = lc.verify_logging()
            assert any("thirdparty.lib" in r for r in repairs), repairs
            assert "LOG_REDACTION_REPAIRED" in alerts.getvalue()
            third.error("password=%s", S_PIN)
            assert lc.verify_logging() == [], "a second pass finds nothing to repair"
        finally:
            third.removeHandler(stray)
            logging.getLogger(lc.__name__).removeHandler(alert)
    _assert_clean(buf.getvalue(), "repaired handler")


def test_verify_wraps_handlers_that_skip_filters():
    buf = io.StringIO()

    class SkipsFilters(logging.StreamHandler):
        def handle(self, record):  # never calls self.filter
            self.emit(record)
            return True

    h = SkipsFilters(buf)
    lg = logging.getLogger("thirdparty.skipper")
    lg.addHandler(h)
    lg.propagate = False
    try:
        repairs = lc.verify_logging()
        assert any("handle wrapped" in r for r in repairs), repairs
        lg.error("password=%s", S_PIN)
    finally:
        lg.removeHandler(h)
    _assert_clean(buf.getvalue(), "wrapped handler")


def test_verify_ignores_library_null_handlers():
    lg = logging.getLogger("thirdparty.quiet")
    h = logging.NullHandler()
    lg.addHandler(h)
    try:
        assert not any("thirdparty.quiet" in r for r in lc.verify_logging())
        assert not h.filters and not hasattr(h, "_redaction_wrapped")
    finally:
        lg.removeHandler(h)


def test_verify_resilences_smartapi():
    from integrations import smartapi

    for lg in smartapi._logzero_loggers():
        lg.disabled = False
    assert not smartapi.is_silenced()
    repairs = lc.verify_logging()
    assert "SmartAPI logging re-silenced" in repairs
    assert smartapi.is_silenced()


# ── Secret registry ──────────────────────────────────────────────────────


def test_registry_register_and_release():
    r = SecretRegistry()
    r.register("a", S_KEY, "abc", None)  # short and None values are ignored
    assert len(r) == 1
    assert r.redact(f"x {S_KEY}") == "x [REDACTED]"
    r.release("a")
    assert r.redact(f"x {S_KEY}") == f"x {S_KEY}"


def test_registry_evicts_oldest_owner_at_cap():
    r = SecretRegistry(max_values=3)
    r.register("old", "aaaa1", "aaaa2")
    r.register("new", "bbbb1", "bbbb2")
    assert len(r) == 2
    assert r.redact("aaaa1") == "aaaa1" and r.redact("bbbb1") == "[REDACTED]"


def test_registry_expires_entries():
    now = [1000.0]
    r = SecretRegistry(ttl_seconds=10, clock=lambda: now[0])
    r.register("a", S_KEY)
    assert r.redact(S_KEY) == "[REDACTED]"
    now[0] += 11
    assert r.redact(S_KEY) == S_KEY and len(r) == 0


def test_registry_is_thread_safe():
    r = SecretRegistry()
    errors = []

    def work(i):
        try:
            for j in range(200):
                r.register(f"o{i}-{j % 5}", f"secret-{i}-{j}")
                r.redact(f"text secret-{i}-{j}")
                if j % 3 == 0:
                    r.release(f"o{i}-{j % 5}")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_failed_login_releases_its_secrets():
    """A rejected login must not leave values in the registry (see session_manager)."""
    import session_manager

    before = len(secret_registry.registry)
    with mock.patch("angel_client.time.sleep"), mock.patch(
        "requests.request",
        return_value=_Response({"status": False, "message": "Invalid", "errorcode": "X",
                                "data": None}),
    ), owned_config_streams():
        try:
            session_manager.SessionManager().create_session(
                "sid-x", S_KEY, "AB1234", S_PIN, S_TOTP_SECRET
            )
        except RuntimeError:
            pass
    assert len(secret_registry.registry) == before


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
