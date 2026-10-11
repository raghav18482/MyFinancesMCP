"""The one door to Yahoo Finance.

Every yfinance call in the app goes through :func:`call`, because Yahoo rate
limits by *outbound IP*, and on a shared cloud host (Render) that budget is
small, shared with other tenants, and easily exhausted. Before this module,
five call sites fired Yahoo requests in parallel with no common limit: one
peer comparison alone could burst ~100 requests.

What this provides:

* **Pacing**: at most ``YAHOO_MAX_CONCURRENT`` requests in flight and at least
  ``YAHOO_MIN_INTERVAL_SEC`` between request starts, process-wide.
* **Circuit breaker**: after Yahoo rate-limits (429) or refuses us (401/403,
  or an empty answer for a symbol it quotes), stop calling it for a cooldown
  that doubles on each repeat, up to 30 minutes. Hammering a rate limiter only
  extends the ban; while open, callers fail fast with a ``retry_after``.
* **Real causes**: yfinance swallows HTTP errors by default
  (``debug.hide_exceptions``), logging them to its own logger and returning an
  empty result that looks exactly like "no data for this symbol". A handler on
  that logger records the swallowed error *on the calling thread*, so each
  call can say what actually went wrong.
* **Proxy**: ``YAHOO_PROXY_URL`` (``http://user:pass@host:port``) routes all
  Yahoo traffic through a proxy, for when the host's IP is blocked outright.
* **Status**: :func:`status` feeds ``/api/research/data-health``, so the cause
  can be confirmed in production without reading logs.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import yfinance as yf

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Causes, as stable codes for logs, API responses and the health endpoint.
RATE_LIMITED = "rate_limited"  # 429 / YFRateLimitError
BLOCKED = "blocked"  # 401/403, invalid crumb, or an empty answer for a quoted symbol
UPSTREAM = "upstream_error"  # Yahoo 5xx
TIMEOUT = "timeout"
NETWORK = "network"  # DNS, refused, TLS, proxy failure
NOT_FOUND = "not_found"  # 404: Yahoo does not know the symbol
UNKNOWN = "unknown"

# Causes that mean "Yahoo is refusing this IP": back off for everyone.
_TRIPS_BREAKER = {RATE_LIMITED, BLOCKED}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


_MAX_CONCURRENT = max(1, int(_env_float("YAHOO_MAX_CONCURRENT", 2)))
_MIN_INTERVAL = max(0.0, _env_float("YAHOO_MIN_INTERVAL_SEC", 0.4))
_COOLDOWN_BASE = _env_float("YAHOO_COOLDOWN_SEC", 120)
_COOLDOWN_MAX = 1800.0


class YahooUnavailable(Exception):
    """Yahoo could not answer. Transient: offer Retry after ``retry_after`` seconds."""

    def __init__(self, cause: str, detail: str, retry_after: int):
        super().__init__(f"{cause}: {detail}")
        self.cause = cause
        self.detail = detail
        self.retry_after = retry_after


class YahooNotFound(Exception):
    """Yahoo answered 404: it has no such symbol. Not transient."""


# ── Swallowed-error capture ──────────────────────────────────────────────────

_local = threading.local()


class _SwallowedErrorHandler(logging.Handler):
    """Keeps the last ERROR yfinance logged on this thread.

    yfinance logs a hidden HTTPError as ``str(e) + response.text`` on the
    thread that made the request, then returns ``None``/empty. Thread-local
    storage ties that message to the call that caused it.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _local.swallowed = record.getMessage()[:500]
        except Exception:  # never let logging break a request
            pass


_handler = _SwallowedErrorHandler(level=logging.ERROR)


def _install_handler() -> None:
    yf_logger = logging.getLogger("yfinance")
    if _handler not in yf_logger.handlers:
        yf_logger.addHandler(_handler)


def take_swallowed_error() -> str | None:
    """The error yfinance hid during this thread's last call, if any."""
    msg = getattr(_local, "swallowed", None)
    _local.swallowed = None
    return msg


# ── Classification ───────────────────────────────────────────────────────────

_STATUS_RE = re.compile(r"\b(401|403|404|429|5\d\d)\b")


def classify(error: BaseException | str) -> str:
    """Map an exception, or a message yfinance logged, to a cause code."""
    from yfinance.exceptions import YFRateLimitError

    if isinstance(error, YFRateLimitError):
        return RATE_LIMITED
    text = str(error)
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status is None:
        m = _STATUS_RE.search(text)
        status = int(m.group(1)) if m else None
    lowered = text.lower()
    if status == 429 or "too many requests" in lowered or "rate limit" in lowered:
        return RATE_LIMITED
    if status in (401, 403) or "invalid crumb" in lowered or "unauthorized" in lowered:
        return BLOCKED
    if status == 404 or "not found" in lowered:
        return NOT_FOUND
    if status is not None and status >= 500:
        return UPSTREAM
    if "timed out" in lowered or "timeout" in lowered:
        return TIMEOUT
    if isinstance(error, (ConnectionError, OSError)) or any(
        s in lowered for s in ("connection", "resolve", "proxy", "ssl", "tls", "curl")
    ):
        return NETWORK
    return UNKNOWN


# ── Breaker and pacing ───────────────────────────────────────────────────────

_lock = threading.Lock()
_slots = threading.BoundedSemaphore(_MAX_CONCURRENT)
_state: dict[str, Any] = {
    "open_until": 0.0,
    "cooldown": 0.0,
    "last_start": 0.0,
    "last_success_at": None,
    "last_failure_at": None,
    "last_failure_cause": None,
    "last_failure_detail": None,
    "calls": 0,
    "failures": 0,
    "short_circuited": 0,
}


def _retry_after(now: float | None = None) -> int:
    now = time.time() if now is None else now
    return max(1, int(_state["open_until"] - now + 0.999))


def _check_breaker() -> None:
    with _lock:
        if time.time() < _state["open_until"]:
            _state["short_circuited"] += 1
            raise YahooUnavailable(
                _state["last_failure_cause"] or RATE_LIMITED,
                "Yahoo Finance is refusing requests from this server; backing off.",
                _retry_after(),
            )


def _pace() -> None:
    """Space request starts at least ``_MIN_INTERVAL`` apart, process-wide."""
    with _lock:
        now = time.time()
        wait = _state["last_start"] + _MIN_INTERVAL - now
        _state["last_start"] = max(now, _state["last_start"] + _MIN_INTERVAL)
    if wait > 0:
        time.sleep(wait)


def record_failure(cause: str, detail: str) -> int:
    """Note a failure; open the breaker for a refusal. Returns retry-after seconds."""
    with _lock:
        now = time.time()
        _state["failures"] += 1
        _state["last_failure_at"] = now
        _state["last_failure_cause"] = cause
        _state["last_failure_detail"] = detail[:300]
        if cause in _TRIPS_BREAKER:
            # Double the cooldown on each consecutive refusal, from the base.
            cooldown = min(_COOLDOWN_MAX, max(_COOLDOWN_BASE, _state["cooldown"] * 2))
            _state["cooldown"] = cooldown
            _state["open_until"] = now + cooldown
            logger.warning(
                "Yahoo %s; pausing Yahoo calls for %ds. Detail: %s",
                cause, int(cooldown), detail[:300],
            )
            return int(cooldown)
        return 30


def record_success() -> None:
    with _lock:
        _state["last_success_at"] = time.time()
        _state["cooldown"] = 0.0
        _state["open_until"] = 0.0


def call(fn: Callable[[], T], *, what: str, symbol: str) -> T:
    """Run one yfinance request under the shared limits.

    Raises :class:`YahooUnavailable` (transient; carries cause and
    retry-after) or :class:`YahooNotFound`. Other exceptions, such as a parse
    error on an odd payload, propagate unchanged.
    """
    _check_breaker()
    with _slots:
        _check_breaker()  # it may have opened while we queued
        _pace()
        with _lock:
            _state["calls"] += 1
        take_swallowed_error()
        try:
            value = fn()
        except Exception as e:
            cause = classify(e)
            if cause == NOT_FOUND:
                raise YahooNotFound(symbol) from e
            if cause == UNKNOWN and not _looks_like_transport(e):
                raise
            detail = f"{what} {symbol}: {type(e).__name__}: {e}"
            raise YahooUnavailable(cause, detail, record_failure(cause, detail)) from e
    return value


def _looks_like_transport(e: BaseException) -> bool:
    module = type(e).__module__ or ""
    return module.startswith(("curl_cffi", "requests", "urllib3", "yfinance"))


def fail_from_swallowed(what: str, symbol: str) -> YahooUnavailable | YahooNotFound | None:
    """After a call returned empty: the error yfinance hid, as our exception."""
    swallowed = take_swallowed_error()
    if not swallowed:
        return None
    cause = classify(swallowed)
    if cause == NOT_FOUND:
        return YahooNotFound(symbol)
    if cause == UNKNOWN:
        cause = BLOCKED
    detail = f"{what} {symbol}: {swallowed}"
    return YahooUnavailable(cause, detail, record_failure(cause, detail))


# ── Configuration and status ─────────────────────────────────────────────────

def _configure_proxy() -> bool:
    proxy = os.environ.get("YAHOO_PROXY_URL", "").strip()
    if not proxy:
        return False
    yf.config.network.proxy = {"http": proxy, "https": proxy}
    host = proxy.rsplit("@", 1)[-1]
    logger.info("Yahoo Finance requests go through proxy %s", host)
    return True


_PROXY_CONFIGURED = _configure_proxy()
_install_handler()


def status() -> dict[str, Any]:
    """A snapshot for the health endpoint. Holds no secrets."""
    with _lock:
        now = time.time()
        open_ = now < _state["open_until"]

        def iso(ts: float | None) -> str | None:
            return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

        return {
            "proxy_configured": _PROXY_CONFIGURED,
            "max_concurrent": _MAX_CONCURRENT,
            "min_interval_sec": _MIN_INTERVAL,
            "breaker_open": open_,
            "retry_after_sec": _retry_after(now) if open_ else 0,
            "last_success_at": iso(_state["last_success_at"]),
            "last_failure_at": iso(_state["last_failure_at"]),
            "last_failure_cause": _state["last_failure_cause"],
            "last_failure_detail": _state["last_failure_detail"],
            "calls": _state["calls"],
            "failures": _state["failures"],
            "short_circuited": _state["short_circuited"],
        }


def reset_for_tests() -> None:
    with _lock:
        _state.update(
            open_until=0.0, cooldown=0.0, last_start=0.0, last_success_at=None,
            last_failure_at=None, last_failure_cause=None, last_failure_detail=None,
            calls=0, failures=0, short_circuited=0,
        )
