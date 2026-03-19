"""
SQLite long-term preferences per user_key (Angel client code or session fallback).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def default_memory_db_path() -> str:
    p = os.environ.get("MEMORY_DB", "")
    if p:
        return p
    root = Path(__file__).resolve().parent.parent
    root.joinpath("data").mkdir(parents=True, exist_ok=True)
    return str(root / "data" / "agent_memory.db")


class UserMemoryStore:
    def __init__(self, db_path: str | None = None):
        self._path = db_path or default_memory_db_path()
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, check_same_thread=False)

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memory (
                    user_key TEXT NOT NULL,
                    mem_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_key, mem_key)
                )
                """
            )
            c.commit()

    def get(self, user_key: str, mem_key: str) -> Any | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM user_memory WHERE user_key = ? AND mem_key = ?",
                (user_key, mem_key),
            ).fetchone()
        if not row:
            return None
        raw = row[0]
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def get_all(self, user_key: str) -> dict[str, Any]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT mem_key, value FROM user_memory WHERE user_key = ?",
                (user_key,),
            ).fetchall()
        out: dict[str, Any] = {}
        for k, v in rows:
            try:
                out[k] = json.loads(v)
            except json.JSONDecodeError:
                out[k] = v
        return out

    def set(self, user_key: str, mem_key: str, value: Any) -> None:
        payload = json.dumps(value, default=str)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO user_memory (user_key, mem_key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_key, mem_key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (user_key, mem_key, payload, ts),
            )
            c.commit()


_store: UserMemoryStore | None = None


def get_memory_store() -> UserMemoryStore:
    global _store
    if _store is None:
        _store = UserMemoryStore()
    return _store
