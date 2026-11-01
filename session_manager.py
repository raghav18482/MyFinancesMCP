import time
import threading
import logging
from typing import Callable, Optional

from angel_client import AngelOneClient

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 8 * 3600  # 8 hours, matching Angel One session lifetime


class SessionManager:
    """Maps session IDs to per-user AngelOneClient instances.

    Credentials are never persisted here — they live only in memory for the
    duration of a session and are discarded on expiry or server restart.

    Keys are chosen by the caller and must come from a verified identity: a
    random web ``sid`` stored in the signed cookie, ``mcp-user-<id>`` derived
    from a verified MCP bearer token, or a scheduler job's throwaway id. Nothing
    here ever takes a key from request parameters or tool arguments.

    Everything is per process. With several workers each keeps its own sessions
    (so one user can hold one broker login per worker) and its own locks.

    Thread-safe: logins run in worker threads (see ``web.broker_sessions``).
    Per-session resources that are *not* thread-safe (ADK runners, feeds,
    caches) are released by the caller on the event loop, from the ids that
    :meth:`cleanup_expired` returns.
    """

    def __init__(self):
        self._sessions: dict[str, AngelOneClient] = {}
        self._created_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        # Sessions gone without their resources released yet: expired on
        # access, or replaced by a rebuild. Drained by cleanup_expired().
        self._pending_release: set[str] = set()

    def create_session(
        self,
        session_id: str,
        api_key: str,
        client_id: str,
        password: str,
        totp_secret: str,
    ) -> AngelOneClient:
        client = AngelOneClient(api_key, client_id, password, totp_secret)
        try:
            client.ensure_session()
        except BaseException:
            # A rejected login keeps nothing registered for redaction, so failed
            # attempts cannot fill the secret registry and evict live secrets.
            _release_secrets(client)
            raise
        with self._lock:
            replaced = self._sessions.get(session_id)
            self._sessions[session_id] = client
            self._created_at[session_id] = time.time()
        if replaced is not None and replaced is not client:
            _release_secrets(replaced)
        logger.info("Session created for client %s (session %s)", client_id, session_id[:8])
        return client

    def key_lock(self, key: str) -> threading.Lock:
        """A lock unique to ``key``, so concurrent first requests log in once."""
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = self._key_locks[key] = threading.Lock()
            return lock

    def get_or_create(self, key: str, factory: Callable[[], Optional[object]]):
        """The live client for ``key``, or the one ``factory`` builds, created at most once.

        ``factory`` runs under a per-key lock and must itself register the
        session (normally by calling :meth:`create_session` with ``key``). A
        second caller that was waiting re-checks and reuses the result.
        """
        client = self.get_client(key)
        if client is not None:
            return client
        with self.key_lock(key):
            client = self.get_client(key)
            if client is not None:
                return client
            return factory()

    def get_client(self, session_id: str) -> Optional[AngelOneClient]:
        with self._lock:
            created = self._created_at.get(session_id)
            if created and (time.time() - created) > SESSION_TTL_SECONDS:
                self._remove_unlocked(session_id)
                # Its runners, feed and caches still exist; release them later.
                self._pending_release.add(session_id)
                return None
            client = self._sessions.get(session_id)
        if client:
            client.ensure_session()
        return client

    def remove_session(self, session_id: str):
        with self._lock:
            self._remove_unlocked(session_id)

    def _remove_unlocked(self, session_id: str):
        _release_secrets(self._sessions.pop(session_id, None))
        self._created_at.pop(session_id, None)
        self._key_locks.pop(session_id, None)
        logger.info("Session removed: %s", session_id[:8])

    def mark_for_release(self, session_id: str) -> None:
        """Queue a session id whose resources must be released (e.g. after a rebuild)."""
        if session_id:
            with self._lock:
                self._pending_release.add(session_id)

    def cleanup_expired(self) -> list[str]:
        """Drop expired sessions; return every id whose resources need releasing.

        That is the sessions expired now plus those queued since the last call
        (expired on access, or marked by :meth:`mark_for_release`). Callers
        release per-session resources such as the market data feed in
        ``services.realtime_feed`` for each id.
        """
        now = time.time()
        with self._lock:
            expired = [
                sid for sid, t in self._created_at.items()
                if (now - t) > SESSION_TTL_SECONDS
            ]
            for sid in expired:
                self._remove_unlocked(sid)
            released = set(expired) | self._pending_release
            self._pending_release.clear()
        if released:
            logger.info("Cleaned up %d expired or replaced sessions", len(released))
        return sorted(released)

    def remove_web_sessions_for(self, client_id: str) -> list[str]:
        """Remove every web session for this broker account; return their ids.

        MCP (``mcp-user-``) and scheduler sessions are left alone: they are
        authorized separately. Used on logout (all devices) and account
        deletion. The caller releases the returned ids' resources.
        """
        prefixes = ("mcp-user-", "sched-", "manual-")
        with self._lock:
            sids = [
                sid for sid, client in self._sessions.items()
                if getattr(client, "client_id", None) == client_id and not sid.startswith(prefixes)
            ]
            for sid in sids:
                self._remove_unlocked(sid)
        return sids

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)


def _release_secrets(client) -> None:
    """Drop a client's values from the log-redaction registry, if it registered any."""
    release = getattr(client, "release_secrets", None)
    if release is not None:
        try:
            release()
        except Exception:
            logger.exception("Releasing redaction secrets failed")


sessions = SessionManager()
