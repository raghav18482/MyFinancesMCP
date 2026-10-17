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
    """

    def __init__(self):
        self._sessions: dict[str, AngelOneClient] = {}
        self._created_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}

    def create_session(
        self,
        session_id: str,
        api_key: str,
        client_id: str,
        password: str,
        totp_secret: str,
    ) -> AngelOneClient:
        client = AngelOneClient(api_key, client_id, password, totp_secret)
        client.ensure_session()
        with self._lock:
            self._sessions[session_id] = client
            self._created_at[session_id] = time.time()
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
                return None
            client = self._sessions.get(session_id)
        if client:
            client.ensure_session()
        return client

    def remove_session(self, session_id: str):
        with self._lock:
            self._remove_unlocked(session_id)

    def _remove_unlocked(self, session_id: str):
        self._sessions.pop(session_id, None)
        self._created_at.pop(session_id, None)
        self._key_locks.pop(session_id, None)
        logger.info("Session removed: %s", session_id[:8])

    def cleanup_expired(self) -> list[str]:
        """Drop expired sessions and return the ids removed.

        Callers use the return value to release per-session resources such as
        the market data feed in ``services.realtime_feed``.
        """
        now = time.time()
        with self._lock:
            expired = [
                sid for sid, t in self._created_at.items()
                if (now - t) > SESSION_TTL_SECONDS
            ]
            for sid in expired:
                self._remove_unlocked(sid)
        if expired:
            logger.info("Cleaned up %d expired sessions", len(expired))
        return expired

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)


sessions = SessionManager()
