"""The process's one logging configuration, with redaction on every handler.

Why this exists
---------------
SmartAPI logs the full request on any failed broker call: headers carrying the
API key and the session JWT, and for a login the client code, PIN and TOTP
(see :mod:`integrations.smartapi`, which silences it at the source). This module
is the second line: whatever any logger emits passes :class:`RedactingFilter`
before it reaches a stream.

How it is kept in force
-----------------------
- :data:`LOGGING` is applied once by :func:`configure_logging` and handed to
  uvicorn as its ``log_config`` (:func:`uvicorn_log_config`), so uvicorn builds
  the same filtered handlers instead of its own unfiltered ones.
- Nothing else in the codebase may configure logging or add handlers;
  ``tests/test_app_wiring.py`` enforces that statically.
- :func:`verify_logging` runs at startup and every cleanup sweep. It attaches
  the filter to any handler that lacks it (a library adding one at runtime),
  re-silences SmartAPI (``dictConfig`` re-enables loggers it does not name), and
  logs ``LOG_REDACTION_REPAIRED`` so a repair is never silent.

The filter never raises and never lets an unredacted record through: if
redaction itself fails, the record is replaced by a placeholder.
"""
from __future__ import annotations

import copy
import logging
import logging.config
import re
import sys
import threading
import traceback
from typing import Any, Optional

from services import secret_registry

REDACTED = "[REDACTED]"
SUPPRESSED = "[log record suppressed: redaction error]"

# ``password: x``, ``'totp': '123456'``, ``api_key=x``, ``X-PrivateKey: x`` ...
_SENSITIVE_KEY = (
    r"password|passwd|pin|totp|totp_secret|secret|api[_-]?key|apikey|x-privatekey|"
    r"authorization|jwt_?token|refresh_?token|feed_?token|access_?token|client_?secret"
)
_KEY_VALUE_RE = re.compile(
    rf"""(?ix)
    (?<![A-Za-z0-9_])            # whole key, so 'spin=' or 'pinned' do not match
    (["']?(?:{_SENSITIVE_KEY})["']?\s*[:=]\s*)
    (["']?)
    (?!\[REDACTED\])
    ([^"'\s,;&)}}\]]+)
    """
)
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+(?!\[REDACTED\])[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_FERNET_RE = re.compile(r"gAAAAA[A-Za-z0-9_=-]{20,}")
_SENSITIVE_DICT_KEY = re.compile(rf"(?i)^(?:{_SENSITIVE_KEY}|cookie|set-cookie)$")

# Attributes every LogRecord has; anything else on a record came from ``extra``.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "_redacted"}

# uvicorn's formatters unpack ``record.args`` themselves, and its error logger
# passes a ``color_message`` template that needs the same args. Records from
# these loggers keep their args tuple, redacted element by element.
_ARG_PRESERVING_LOGGERS = ("uvicorn",)


def redact(text: Optional[str]) -> Optional[str]:
    """``text`` with credential patterns and registered secret values removed."""
    if not text or not isinstance(text, str):
        return text
    # Bearer first: otherwise "Authorization: Bearer <jwt>" would redact the
    # word "Bearer" as the header's value and leave the token behind.
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _FERNET_RE.sub(REDACTED, text)
    return secret_registry.redact_known(text)


def redact_obj(value: Any, _depth: int = 0) -> Any:
    """A redacted copy of ``value``; sensitive dict keys lose their values entirely."""
    if _depth > 8:
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and _SENSITIVE_DICT_KEY.match(k)
                else redact_obj(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [redact_obj(v, _depth + 1) for v in value]
        if isinstance(value, (set, frozenset)):
            return items
        try:
            return type(value)(items)
        except TypeError:  # e.g. a namedtuple
            return tuple(items)
    # Any other object is logged through its repr, which may carry a secret.
    return redact(repr(value))


class RedactingFilter(logging.Filter):
    """Redacts message, args, ``extra`` fields, exception chain and stack info."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.__dict__.get("_redacted"):
            return True
        try:
            self._redact(record)
        except Exception:
            _suppress(record)
        record._redacted = True
        return True

    @staticmethod
    def _redact(record: logging.LogRecord) -> None:
        if record.name.startswith(_ARG_PRESERVING_LOGGERS) and isinstance(record.args, tuple):
            record.msg = redact(str(record.msg))
            record.args = tuple(redact_obj(a) for a in record.args)
        else:
            try:
                message = record.getMessage()
            except Exception:  # a malformed log call; keep what it said
                message = f"{record.msg} {record.args!r}"
            record.msg = redact(message)
            record.args = ()
            # A colour template would now be formatted without its args.
            record.__dict__.pop("color_message", None)

        if record.exc_info:
            etype, evalue, tb = record.exc_info
            text = "".join(
                traceback.TracebackException(
                    etype, evalue, tb, capture_locals=False
                ).format(chain=True)
            ).rstrip("\n")
            record.exc_text = redact(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)

        if record.stack_info:
            record.stack_info = redact(record.stack_info)

        for key in [k for k in record.__dict__ if k not in _STANDARD_ATTRS]:
            record.__dict__[key] = redact_obj(record.__dict__[key])


def _suppress(record: logging.LogRecord) -> None:
    """Replace a record whose redaction failed. Its content is never emitted."""
    record.msg = SUPPRESSED
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    for key in [k for k in record.__dict__ if k not in _STANDARD_ATTRS]:
        del record.__dict__[key]


# ── Configuration ─────────────────────────────────────────────────────────

_REDACT = {"redact": {"()": "services.logging_config.RedactingFilter"}}
# Above CRITICAL: nothing these loggers produce is ever handled.
_SILENT = {"level": logging.CRITICAL + 50, "handlers": [], "propagate": False}

LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": _REDACT,
    "formatters": {
        # The two names uvicorn expects; it may set ``use_colors`` on them.
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": None,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        },
        "plain": {"format": "%(levelname)s:%(name)s:%(message)s"},
    },
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stderr",
            "filters": ["redact"],
        },
        "access": {
            "class": "logging.StreamHandler",
            "formatter": "access",
            "stream": "ext://sys.stdout",
            "filters": ["redact"],
        },
        "root": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
            "stream": "ext://sys.stderr",
            "filters": ["redact"],
        },
    },
    "root": {"handlers": ["root"], "level": "INFO"},
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        # SmartAPI: logzero's logger and its stdlib loggers. See integrations/smartapi.py.
        "logzero_default": dict(_SILENT),
        "SmartApi": dict(_SILENT),
    },
}

_configured = False
_configure_lock = threading.Lock()


def build_config(fmt: Optional[str] = None, level: str = "INFO") -> dict[str, Any]:
    """A fresh copy of :data:`LOGGING`, optionally with a different root format."""
    config = copy.deepcopy(LOGGING)
    if fmt:
        config["formatters"]["plain"] = {"format": fmt}
    config["root"]["level"] = level
    return config


def configure_logging(fmt: Optional[str] = None, level: str = "INFO") -> None:
    """Apply the redacting configuration once per process (later calls are no-ops)."""
    global _configured
    with _configure_lock:
        if _configured:
            return
        apply_config(build_config(fmt, level))
        _configured = True


def apply_config(config: dict[str, Any]) -> None:
    """``dictConfig`` then re-silence SmartAPI.

    ``dictConfig`` sets ``disabled = False`` on every logger it touches or does
    not name, SmartAPI's included, so silencing must always follow it.
    """
    logging.config.dictConfig(config)
    _silence_smartapi()


def uvicorn_log_config() -> dict[str, Any]:
    """The config to pass as ``uvicorn.run(log_config=...)``; uvicorn may mutate it."""
    return build_config()


def _silence_smartapi() -> None:
    smartapi = sys.modules.get("integrations.smartapi")
    if smartapi is not None:
        smartapi.silence()


def after_uvicorn_configured() -> list[str]:
    """Startup hook: uvicorn's ``dictConfig`` re-enabled SmartAPI's loggers, as it
    always does, so re-silence them quietly, then verify. Only unexpected repairs
    are reported (``LOG_REDACTION_REPAIRED``)."""
    _silence_smartapi()
    return verify_logging()


def _all_loggers() -> list[logging.Logger]:
    loggers = [logging.getLogger()]
    loggers += [
        lg for lg in logging.Logger.manager.loggerDict.values()
        if isinstance(lg, logging.Logger)
    ]
    return loggers


def _wrap_handle(handler: logging.Handler) -> None:
    """Make a handler whose ``handle`` skips filters run ours first anyway."""
    original = handler.handle
    redactor = RedactingFilter()

    def handle(record: logging.LogRecord):
        redactor.filter(record)
        return original(record)

    handler.handle = handle  # type: ignore[method-assign]
    handler._redaction_wrapped = True  # type: ignore[attr-defined]


def verify_logging() -> list[str]:
    """Re-establish redaction everywhere; return what had to be repaired.

    Every repair is logged as ``LOG_REDACTION_REPAIRED``, an alert, because it
    means something configured logging outside :data:`LOGGING`.
    """
    repairs: list[str] = []
    for lg in _all_loggers():
        for h in list(lg.handlers):
            # Libraries attach NullHandler by convention; it never writes anything.
            if type(h) is logging.NullHandler:
                continue
            label = f"{lg.name or 'root'}:{type(h).__name__}"
            if not any(isinstance(f, RedactingFilter) for f in h.filters):
                h.addFilter(RedactingFilter())
                repairs.append(f"filter added to {label}")
            if type(h).handle is not logging.Handler.handle and not getattr(
                h, "_redaction_wrapped", False
            ):
                _wrap_handle(h)
                repairs.append(f"handle wrapped on {label}")

    smartapi = sys.modules.get("integrations.smartapi")
    if smartapi is not None and not smartapi.is_silenced():
        smartapi.silence()
        repairs.append("SmartAPI logging re-silenced")

    if repairs:
        logging.getLogger(__name__).error("LOG_REDACTION_REPAIRED %s", "; ".join(repairs))
    return repairs
