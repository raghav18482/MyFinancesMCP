"""The public feedback form with WhatsApp replies off (the default) and on.

Off must mean off: no phone number read, no LLM called, no message sent; the
feedback is stored with no personal data instead.

Run directly: ``python tests/test_feedback_relay.py``.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
from datetime import timedelta
from unittest import mock

from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PHONE = "+91 00000 12345"


@contextlib.contextmanager
def app(env: dict[str, str] | None = None):
    """The app over a temp SQLite DB, with the LLM, phone normalizer and WhatsApp mocked."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session, SQLModel, create_engine

    import db
    from db import models  # noqa: F401
    from services import feedback
    from services.schedular import whatsapp
    from web.app import create_app

    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
    tmp = tempfile.TemporaryDirectory()
    engine = create_engine(f"sqlite:///{os.path.join(tmp.name, 't.db')}")
    SQLModel.metadata.create_all(engine)
    feedback._rate_hits.clear()
    feedback._used_nonces.clear()

    mocks = {
        "llm": mock.AsyncMock(return_value="Thanks!"),
        "send": mock.AsyncMock(return_value=None),
        "normalise": mock.Mock(side_effect=lambda p: "910000012345"),
    }
    base = {k: v for k, v in os.environ.items() if k != "FEEDBACK_WHATSAPP_REPLY"}
    with mock.patch.dict(os.environ, {**base, **(env or {})}, clear=True), \
            mock.patch.object(db, "get_session", lambda: Session(engine)), \
            mock.patch.object(feedback, "generate_reply", mocks["llm"]), \
            mock.patch.object(whatsapp, "_post", mocks["send"]), \
            mock.patch.object(whatsapp, "_normalise_phone", mocks["normalise"]):
        with TestClient(create_app()) as client:
            yield client, mocks, engine
    tmp.cleanup()


def _submit(client, **extra):
    token = client.get("/api/feedback/token").json()["token"]
    body = {"token": token, "feedback": "Love the dashboard", "phone": PHONE, "name": "Asha", **extra}
    return client.post("/api/feedback", json=body)


def _rows(engine):
    from sqlalchemy import text

    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text("SELECT * FROM feedback_submissions"))]


def test_off_for_every_value_but_exactly_1():
    for value in (None, "", "0", "false", "true", "yes", "on", " 1", "1 "):
        env = {} if value is None else {"FEEDBACK_WHATSAPP_REPLY": value}
        with app(env) as (client, mocks, engine):
            assert client.get("/api/feedback/token").json()["whatsapp_reply"] is False
            res = _submit(client, channel="react")
            assert res.status_code == 200, (value, res.text)
            assert mocks["llm"].await_count == 0, value
            assert mocks["send"].await_count == 0, value
            assert mocks["normalise"].call_count == 0, value
            rows = _rows(engine)
            assert len(rows) == 1 and rows[0]["channel"] == "react", rows


def test_stored_row_holds_no_personal_data_and_nothing_is_logged():
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    grab = Grab()
    logging.getLogger().addHandler(grab)
    try:
        with app() as (client, _mocks, engine):
            _submit(client, channel="jinja")
            (row,) = _rows(engine)
    finally:
        logging.getLogger().removeHandler(grab)
    assert set(row) == {"id", "feedback", "channel", "created_at"}, row
    assert "Asha" not in str(row) and "12345" not in str(row)
    assert not any("12345" in r for r in records)


def test_unknown_channel_is_recorded_as_api():
    with app() as (client, _mocks, engine):
        _submit(client, channel="email")
        _submit(client)
        assert [r["channel"] for r in _rows(engine)] == ["api", "api"]


def test_daily_cap_refuses_after_the_limit():
    with app({"FEEDBACK_DAILY_CAP": "2"}) as (client, _mocks, engine):
        import services.feedback as fb

        for _ in range(2):
            fb._rate_hits.clear()
            assert _submit(client).status_code == 200
        fb._rate_hits.clear()
        res = _submit(client)
        assert res.status_code == 429, res.text
        assert len(_rows(engine)) == 2


def test_reply_mode_uses_the_llm_and_whatsapp_once():
    with app({"FEEDBACK_WHATSAPP_REPLY": "1"}) as (client, mocks, engine):
        assert client.get("/api/feedback/token").json()["whatsapp_reply"] is True
        res = _submit(client)
        assert res.status_code == 200, res.text
        assert mocks["llm"].await_count == 1 and mocks["send"].await_count == 1
        assert _rows(engine) == [], "reply mode does not store"


def test_jinja_landing_hides_the_phone_field_when_off():
    with app() as (client, _mocks, _engine):
        html = client.get("/").text
    assert 'name="phone"' not in html and "Get a reply on WhatsApp" not in html
    assert "channel: 'jinja'" in html
    with app({"FEEDBACK_WHATSAPP_REPLY": "1"}) as (client, _mocks, _engine):
        html = client.get("/").text
    assert 'name="phone"' in html and "+91 00000 00000" in html


def test_retention_purge_is_off_until_configured():
    from sqlalchemy import text

    from db.models import utcnow
    from services import feedback

    with app() as (client, _mocks, engine):
        _submit(client)
        with engine.begin() as c:
            c.execute(text("UPDATE feedback_submissions SET created_at = :t"),
                      {"t": utcnow() - timedelta(days=40)})
        assert feedback.purge_old_submissions() == 0, "unset: nothing purged"
        with mock.patch.dict(os.environ, {"FEEDBACK_RETENTION_DAYS": "abc"}):
            assert feedback.purge_old_submissions() == 0
        with mock.patch.dict(os.environ, {"FEEDBACK_RETENTION_DAYS": "60"}):
            assert feedback.purge_old_submissions() == 0, "40 days is inside 60"
        with mock.patch.dict(os.environ, {"FEEDBACK_RETENTION_DAYS": "30"}):
            assert feedback.purge_old_submissions() == 1
        assert _rows(engine) == []


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
