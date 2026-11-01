"""The only module that imports Angel One's SmartAPI SDK, and it silences its logging.

``SmartConnect._request`` (smartapi-python 1.5.5) logs every failed call with
``Headers: {...}, Request: {...}``: the API key (``X-PrivateKey``), the session
JWT, and for a login the client code, PIN and TOTP. It logs through
``logzero``, at ERROR, to stderr and to ``logs/<date>/app.log``.
``SmartConnect.__init__`` and ``SmartWebSocketV2.__init__`` re-attach that file
logger every time one is built.

SmartAPI is the only installed package that uses logzero, so its loggers are
switched off entirely rather than filtered: ``disabled``, a level above
CRITICAL, no file handler. Broker failures are logged instead by
``angel_client`` and ``services.realtime_feed`` with our own redacted messages.

Build clients through :func:`new_smart_connect` and :func:`new_websocket`, which
re-silence after construction. ``tests/test_app_wiring.py`` fails if anything
else imports ``SmartApi`` or ``logzero``.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

import logzero
from SmartApi import smartConnect as _smart_connect_module
from SmartApi import smartWebSocketOrderUpdate as _order_update_module
from SmartApi import smartWebSocketV2 as _websocket_module

SmartConnect = _smart_connect_module.SmartConnect
SmartWebSocketV2 = _websocket_module.SmartWebSocketV2

_SILENT_LEVEL = logging.CRITICAL + 50


def _logzero_loggers() -> list[logging.Logger]:
    """Every logzero logger object SmartAPI can write to.

    Each SmartAPI module bound ``from logzero import logger`` at import time, so
    if anything later replaced ``logzero.logger`` they would differ.
    """
    found: dict[int, logging.Logger] = {}
    for lg in (
        logzero.logger,
        getattr(_smart_connect_module, "logger", None),
        getattr(_websocket_module, "logger", None),
        getattr(_order_update_module, "logger", None),
    ):
        if isinstance(lg, logging.Logger):
            found[id(lg)] = lg
    return list(found.values())


def silence() -> None:
    """Turn SmartAPI's logging off. Idempotent; safe to call from any thread."""
    for lg in _logzero_loggers():
        lg.disabled = True
        lg.setLevel(_SILENT_LEVEL)
        lg.propagate = False
        for h in list(lg.handlers):
            if isinstance(h, RotatingFileHandler):
                lg.removeHandler(h)
                h.close()
    std = logging.getLogger("SmartApi")
    std.setLevel(_SILENT_LEVEL)
    std.propagate = False


def is_silenced() -> bool:
    std = logging.getLogger("SmartApi")
    return (
        all(
            lg.disabled
            and lg.level >= _SILENT_LEVEL
            and not any(isinstance(h, RotatingFileHandler) for h in lg.handlers)
            for lg in _logzero_loggers()
        )
        and std.level >= _SILENT_LEVEL
        and not std.propagate
    )


def new_smart_connect(**kwargs) -> SmartConnect:
    client = SmartConnect(**kwargs)
    silence()
    return client


def new_websocket(*args, **kwargs) -> SmartWebSocketV2:
    ws = SmartWebSocketV2(*args, **kwargs)
    silence()
    return ws


silence()
