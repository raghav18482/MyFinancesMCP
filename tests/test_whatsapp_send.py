"""Tests for services.schedular.whatsapp.send retry behaviour.

The webhook service sleeps when idle: it answers slowly, then 503s while it
reconnects to WhatsApp. No network access required.

Run directly (``python tests/test_whatsapp_send.py``) or under pytest.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.schedular.whatsapp as wa


def _run(responses, *, deadline=5.0, delay=0.0):
    """Send through a mock transport replaying ``responses``; return (error, calls)."""
    calls = []

    def handler(request):
        calls.append(request)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, json={})

    saved = (wa._transport, wa._SEND_DEADLINE_SEC, wa._RETRY_DELAY_SEC)
    wa._transport = httpx.MockTransport(handler)
    wa._SEND_DEADLINE_SEC, wa._RETRY_DELAY_SEC = deadline, delay
    try:
        asyncio.run(wa.send("8107037133", "hi"))
        return None, calls
    except RuntimeError as e:
        return e, calls
    finally:
        wa._transport, wa._SEND_DEADLINE_SEC, wa._RETRY_DELAY_SEC = saved


def test_waking_service_eventually_sends():
    timeout = httpx.ReadTimeout("cold start")
    err, calls = _run([timeout, 503, 503, 200])
    assert err is None
    assert len(calls) == 4


def test_client_error_is_not_retried():
    err, calls = _run([400])
    assert err is not None and "400" in str(err)
    assert len(calls) == 1


def test_gives_up_at_deadline():
    err, calls = _run([503], deadline=0.2, delay=0.05)
    assert err is not None and "503" in str(err)
    assert 1 < len(calls) < 10


def test_payload_shape():
    err, calls = _run([200])
    assert err is None
    assert calls[0].read() == b'{"phone":"918107037133","messages":["hi"]}'


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
