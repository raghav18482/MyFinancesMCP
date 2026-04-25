"""Persistence layer: SQLModel + Postgres + Fernet-encrypted credentials.

Public surface kept tiny on purpose:
- ``engine`` / ``get_session`` / ``init_db``
- model classes (``User``, ``Schedule``, ``Log``)
- ``encrypt_value`` / ``decrypt_value``
"""
from __future__ import annotations

from .crypto import decrypt_value, encrypt_value
from .engine import DATABASE_URL, engine, get_session, init_db
from .models import Log, Schedule, User

__all__ = [
    "DATABASE_URL",
    "engine",
    "get_session",
    "init_db",
    "User",
    "Schedule",
    "Log",
    "encrypt_value",
    "decrypt_value",
]
