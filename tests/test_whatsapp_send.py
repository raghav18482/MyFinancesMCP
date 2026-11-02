"""Tests for services.schedular.whatsapp: outcome classification and retries.

Only a certain "not sent" is retried (a failed connection, or the webhook's
own "not ready" 503). Anything that may have been sent is ``unknown`` and is
never retried. No network access required.

Run directly: ``python tests/test_whatsapp_send.py``.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest import mock

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.schedular.whatsapp as wa

NOT_READY = ("503-ready", 503, {"x-wws-outcome": "not_sent"},
             {"error": "WhatsApp not connected yet", "outcome": "not_sent"})


def _resp(item):
    if isinstance(item, int):
        return httpx.Response(item, json={})
    _name, status, headers, body = item
    return httpx.Response(status, headers=headers, json=body)


def _run(responses, *, deadline=5.0, delay=0.0, env=None):
    """Send through a mock transport replaying ``responses``; return (error, calls)."""
    calls = []

    def handler(request):
        calls.append(request)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return _resp(item)

    saved = (wa._transport, wa._SEND_DEADLINE_SEC, wa._RETRY_DELAY_SEC)
    wa._transport = httpx.MockTransport(handler)
    wa._SEND_DEADLINE_SEC, wa._RETRY_DELAY_SEC = deadline, delay
    base = {k: v for k, v in os.environ.items() if k not in ("WHATSAPP_WEBHOOK_TOKEN", "WHATSAPP_WEBHOOK_URL")}
    try:
        with mock.patch.dict(os.environ, {**base, **(env or {})}, clear=True):
            asyncio.run(wa._post("0000000000", "hi"))
        return None, calls
    except wa.DeliveryError as e:
        return e, calls
    finally:
        wa._transport, wa._SEND_DEADLINE_SEC, wa._RETRY_DELAY_SEC = saved


def test_2xx_is_accepted_once():
    err, calls = _run([200])
    assert err is None and len(calls) == 1


def test_connect_errors_and_not_ready_are_retried():
    err, calls = _run([httpx.ConnectError("down"), httpx.ConnectTimeout("slow"), NOT_READY, 200])
    assert err is None
    assert len(calls) == 4


def test_read_timeout_is_unknown_and_not_retried():
    err, calls = _run([httpx.ReadTimeout("cold start"), 200])
    assert isinstance(err, wa.DeliveryUnknown) and err.outcome == wa.UNKNOWN
    assert len(calls) == 1


def test_dropped_connection_is_unknown():
    err, calls = _run([httpx.RemoteProtocolError("reset"), 200])
    assert isinstance(err, wa.DeliveryUnknown)
    assert len(calls) == 1


def test_500_is_unknown_and_not_retried():
    err, calls = _run([500, 200])
    assert isinstance(err, wa.DeliveryUnknown) and err.reason == "http_5xx"
    assert len(calls) == 1


def test_503_without_the_webhook_signal_is_unknown():
    # A 503 from Render's edge, or one missing the header or the error text.
    for item in (503,
                 ("hdr-only", 503, {"x-wws-outcome": "not_sent"}, {}),
                 ("body-only", 503, {}, {"error": "WhatsApp not connected yet"})):
        err, calls = _run([item, 200])
        assert isinstance(err, wa.DeliveryUnknown), item
        assert len(calls) == 1, item


def test_4xx_is_not_sent_and_not_retried():
    err, calls = _run([400, 200])
    assert isinstance(err, wa.DeliveryNotSent) and err.reason == "http_4xx"
    assert len(calls) == 1
    err, _ = _run([401])
    assert isinstance(err, wa.DeliveryNotSent) and err.reason == "auth"


def test_inconsistent_outcome_is_unknown():
    err, _ = _run([("2xx-said-not-sent", 200, {"x-wws-outcome": "not_sent"}, {})])
    assert isinstance(err, wa.DeliveryUnknown) and err.reason == "inconsistent"
    err, _ = _run([("2xx-body-unknown", 200, {}, {"outcome": "unknown"})])
    assert isinstance(err, wa.DeliveryUnknown)
    err, _ = _run([("4xx-said-accepted", 400, {"x-wws-outcome": "accepted"}, {})])
    assert isinstance(err, wa.DeliveryUnknown)


def test_not_ready_gives_up_at_deadline_as_not_sent():
    err, calls = _run([NOT_READY], deadline=0.2, delay=0.05)
    assert isinstance(err, wa.DeliveryNotSent) and err.reason == "not_ready"
    assert 1 < len(calls) < 10


def test_bad_phone_is_not_sent_without_a_request():
    calls = []
    wa_transport = wa._transport
    wa._transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    try:
        try:
            asyncio.run(wa._post("12345", "hi"))
        except wa.DeliveryNotSent as e:
            assert e.reason == "bad_phone" and "12345" not in str(e)
        else:
            raise AssertionError("expected DeliveryNotSent")
    finally:
        wa._transport = wa_transport
    assert calls == []


def test_payload_shape_and_token():
    err, calls = _run([200])
    assert err is None
    assert calls[0].read() == b'{"phone":"910000000000","messages":["hi"]}'
    assert "authorization" not in calls[0].headers
    err, calls = _run([200], env={"WHATSAPP_WEBHOOK_TOKEN": "t" * 40})
    assert calls[0].headers["authorization"] == "Bearer " + "t" * 40



def test_the_webhook_url_can_be_pointed_at_staging():
    err, calls = _run([200])
    assert str(calls[0].url) == wa._DEFAULT_SEND_URL
    err, calls = _run([200], env={"WHATSAPP_WEBHOOK_URL": "https://wws-staging.example.com/send"})
    assert err is None and str(calls[0].url) == "https://wws-staging.example.com/send"
    err, calls = _run([200], env={"WHATSAPP_WEBHOOK_URL": "http://127.0.0.1:3000/send"})
    assert err is None and calls[0].url.host == "127.0.0.1"
    err, calls = _run([200], env={"WHATSAPP_WEBHOOK_URL": "http://wws.example.com/send"})
    assert isinstance(err, wa.DeliveryNotSent) and calls == [], "plain http refused, nothing sent"

if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
