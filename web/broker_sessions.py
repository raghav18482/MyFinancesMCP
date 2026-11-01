"""Angel One logins off the event loop: one per user at a time, bounded overall.

An Angel One login is a blocking network call (seconds, more when retried).
Run on the event loop it would stall every request in the process, so logins
run in worker threads, and everything that is not thread-safe happens on the
loop: writing ``request.session``, releasing ADK runners and feeds.

:class:`BrokerRuntime`

- **Lifecycle.** One runtime per event loop, kept in a ``WeakKeyDictionary``
  keyed by the loop object itself (never ``id(loop)``, which can be reused), so
  a closed and collected loop takes its runtime with it. :func:`get_runtime`
  creates it on first use *inside* the running loop, which is also where its
  anyio limiters are built: limiters created outside a loop bind to whichever
  loop uses them first. The app lifespan creates it eagerly and calls
  :meth:`stop` on shutdown; a stopped runtime is replaced on next use.
- **Single-flight.** Concurrent logins under one key (``user:<id>`` or
  ``enroll:<client id>``) share one task, awaited through ``asyncio.shield`` so a
  cancelled caller does not cancel it for the others. A done-callback removes
  the task and always retrieves its exception, so a failure nobody waited for
  is logged rather than reported as "never retrieved".
- **Bounded.** ``BROKER_LOGIN_CONCURRENCY`` logins at once (default 4); MCP tools
  get their own limiter (``MCP_TOOL_CONCURRENCY``, default 16) so they cannot
  exhaust anyio's shared thread pool.
- **Memo.** After a cookie's broker session is rebuilt, other requests carrying
  the same expired cookie within 30 seconds reuse the new session. An entry is
  used only while that session is alive, belongs to the same broker account,
  and the user's ``session_version`` has not moved (logout elsewhere).

Everything here is per process; the app runs as a single instance
(``services.instance_registry``).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import threading
import os
import time
import uuid
import weakref
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Optional

import anyio

logger = logging.getLogger(__name__)

MEMO_TTL_SECONDS = 30.0


class BrokerRuntimeUnavailable(RuntimeError):
    """The runtime is not running on this event loop."""


@dataclass(frozen=True)
class _Memo:
    sid: str
    client_id: str
    session_version: int
    expires_at: float


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


class BrokerRuntime:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.closed = True
        self.login_limiter: Optional[anyio.CapacityLimiter] = None
        self.mcp_limiter: Optional[anyio.CapacityLimiter] = None
        self._inflight: dict[str, asyncio.Task] = {}
        self._memo: dict[int, _Memo] = {}

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Bind to the running loop. Must be called from inside it."""
        self.loop = asyncio.get_running_loop()
        self.login_limiter = anyio.CapacityLimiter(_int_env("BROKER_LOGIN_CONCURRENCY", 4))
        self.mcp_limiter = anyio.CapacityLimiter(_int_env("MCP_TOOL_CONCURRENCY", 16))
        self._inflight.clear()
        self._memo.clear()
        self.closed = False

    async def stop(self) -> None:
        self.closed = True
        tasks = list(self._inflight.values())
        self._inflight.clear()
        self._memo.clear()
        for t in tasks:
            t.cancel()

    @property
    def running(self) -> bool:
        if self.closed or self.loop is None:
            return False
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    # ── logins ───────────────────────────────────────────────────────────

    async def login(self, key: str, creds_loader: Callable[[], Any]) -> Optional[tuple[str, Any]]:
        """Log in (once per ``key`` at a time) and register a new broker session.

        ``creds_loader`` runs in a worker thread and returns the credentials, or
        ``None`` if there are none. Returns ``(sid, client)``, or ``None`` when
        there were no credentials. Raises whatever the login raises
        (``angel_client.AngelAuthError`` for a refused or unreachable broker).
        """
        if self.closed:
            raise BrokerRuntimeUnavailable("broker runtime is stopped")
        task = self._inflight.get(key)
        if task is None:
            task = self.loop.create_task(self._login(key, creds_loader))
            self._inflight[key] = task
            task.add_done_callback(partial(self._on_done, key))
        return await asyncio.shield(task)

    async def _login(self, key: str, creds_loader: Callable[[], Any]) -> Optional[tuple[str, Any]]:
        from session_manager import sessions

        creds = await anyio.to_thread.run_sync(creds_loader)
        if creds is None:
            return None
        handoff = _Handoff()
        try:
            sid, client = await anyio.to_thread.run_sync(
                partial(_login_in_thread, key, creds, handoff), limiter=self.login_limiter,
            )
        except asyncio.CancelledError:
            # Only stop() cancels this task (callers go through shield). The
            # thread may still be running, or may have finished a moment ago;
            # either way a session it creates must not outlive this task.
            handoff.abandon()
            raise
        if self.closed:
            # Shut down while the thread ran: nobody will hold this session.
            sessions.remove_session(sid)
            return None
        return sid, client

    def _on_done(self, key: str, task: asyncio.Task) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if task.cancelled():
            return
        exc = task.exception()  # always retrieved, even with no one awaiting
        if exc is not None:
            logger.warning("Broker login for %s failed: %s", key.split(":")[0], exc)

    # ── memo of rebuilt cookie sessions ──────────────────────────────────

    def remember(self, user_id: int, sid: str, client_id: str, session_version: int) -> None:
        self._memo[user_id] = _Memo(sid, client_id, session_version,
                                    self._clock() + MEMO_TTL_SECONDS)

    def recall(self, user_id: int, client_id: str, session_version: int) -> Optional[tuple[str, Any]]:
        """A recently rebuilt session for this user, if it is still valid."""
        from session_manager import sessions

        memo = self._memo.get(user_id)
        if memo is None:
            return None
        if (
            self._clock() >= memo.expires_at
            or memo.client_id != client_id
            or memo.session_version != session_version
        ):
            self._memo.pop(user_id, None)
            return None
        client = sessions.get_client(memo.sid)
        if client is None or client.client_id != client_id:
            self._memo.pop(user_id, None)
            return None
        return memo.sid, client

    def forget_user(self, user_id: int) -> None:
        self._memo.pop(user_id, None)

    def purge_memo(self) -> None:
        now = self._clock()
        for uid in [u for u, m in self._memo.items() if now >= m.expires_at]:
            del self._memo[uid]


# Process-wide, below the per-loop single-flight: a login under a key waits for
# any other thread logging in under the same key, then reuses its session if it
# is under MEMO_TTL_SECONDS old and was made with exactly the same credentials.
# This keeps "one login per user" even across event loops (tests run one per
# client), and the credential check means a reused session is only ever handed
# to a caller who proved the same secrets.
_recent_logins: dict[str, tuple[str, float, tuple[str, str, str, str]]] = {}
_recent_lock = threading.Lock()


def _fingerprint(creds) -> tuple[str, str, str, str]:
    return (str(creds.api_key), str(creds.client_id), str(creds.password), str(creds.totp_secret))


def _same(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    return len(a) == len(b) and all(
        hmac.compare_digest(x.encode(), y.encode()) for x, y in zip(a, b)
    )


class _Handoff:
    """Who removes a session made for a login task that was cancelled meanwhile.

    The thread records the session it created; the task marks itself abandoned
    when cancelled. Under one lock, whichever happens second removes the session,
    so it is removed exactly once whatever the order.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sid: Optional[str] = None
        self._abandoned = False

    def created(self, sid: str) -> None:
        with self._lock:
            self._sid = sid
            abandoned = self._abandoned
        if abandoned:
            _remove(sid)

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            sid = self._sid
        if sid is not None:
            _remove(sid)


def _remove(sid: str) -> None:
    from session_manager import sessions

    sessions.remove_session(sid)


def _login_in_thread(key: str, creds, handoff: Optional[_Handoff] = None) -> tuple[str, Any]:
    """Runs in a worker thread: log in once per key, or reuse a fresh session.

    A newly created session is reported to ``handoff`` (a reused one is not:
    it belongs to whoever created it).
    """
    from session_manager import sessions

    fp = _fingerprint(creds)
    with sessions.key_lock(f"login:{key}"):
        with _recent_lock:
            recent = _recent_logins.get(key)
        if recent and time.monotonic() - recent[1] < MEMO_TTL_SECONDS and _same(recent[2], fp):
            client = sessions.get_client(recent[0])
            if client is not None and client.client_id == creds.client_id:
                return recent[0], client
        sid = uuid.uuid4().hex
        client = sessions.create_session(sid, *fp)
        if handoff is not None:
            handoff.created(sid)
        with _recent_lock:
            _recent_logins[key] = (sid, time.monotonic(), fp)
            for k in [k for k, v in _recent_logins.items()
                      if time.monotonic() - v[1] >= MEMO_TTL_SECONDS]:
                del _recent_logins[k]
        return sid, client


_runtimes: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, BrokerRuntime]" = (
    weakref.WeakKeyDictionary()
)


def get_runtime() -> BrokerRuntime:
    """The runtime for the running loop, created and started on first use.

    Raises :class:`BrokerRuntimeUnavailable` outside a running loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        raise BrokerRuntimeUnavailable("no running event loop") from None
    rt = _runtimes.get(loop)
    if rt is None or rt.closed:
        rt = BrokerRuntime()
        rt.start()
        _runtimes[loop] = rt
    return rt


def mcp_limiter() -> Optional[anyio.CapacityLimiter]:
    """The MCP tool limiter for the running loop, or ``None`` outside one."""
    try:
        return get_runtime().mcp_limiter
    except BrokerRuntimeUnavailable:
        return None


def reset_for_tests() -> None:
    """Forget every runtime and recent login (tests only)."""
    _runtimes.clear()
    with _recent_lock:
        _recent_logins.clear()
