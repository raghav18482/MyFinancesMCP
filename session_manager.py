import time
import threading
import logging
from typing import Optional

from angel_client import AngelOneClient

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 8 * 3600  # 8 hours, matching Angel One session lifetime


class SessionManager:
    """Maps session IDs to per-user AngelOneClient instances.

    Credentials are never persisted — they live only in memory for the
    duration of a session and are discarded on expiry or server restart.
    """

    def __init__(self):
        self._sessions: dict[str, AngelOneClient] = {}
        self._created_at: dict[str, float] = {}
        self._lock = threading.Lock()

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
        logger.info("Session removed: %s", session_id[:8])

    def cleanup_expired(self):
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

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)


sessions = SessionManager()
