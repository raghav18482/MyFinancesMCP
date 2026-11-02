"""Persistence layer: SQLModel + Postgres + Fernet-encrypted credentials.

Public surface kept tiny on purpose:
- ``engine`` / ``get_session`` / ``init_db``
- model classes (``User``, ``Schedule``, ``Log``)
- ``encrypt_value`` / ``decrypt_value`` — reversible, for broker credentials
- tokens: ``db.crypto.seal_token`` / ``open_token`` (their own key ring)
- ``hash_password`` / ``verify_password`` — one-way, for the app's own login
"""
from __future__ import annotations

from .crypto import decrypt_value, encrypt_value
from .engine import DATABASE_URL, engine, get_session, init_db
from .models import ChatThread, Log, McpToken, RiskProfile, Schedule, User
from .passwords import (
    MIN_PASSWORD_CHARS,
    hash_password,
    needs_rehash,
    verify_password,
)

__all__ = [
    "DATABASE_URL",
    "engine",
    "get_session",
    "init_db",
    "User",
    "Schedule",
    "Log",
    "RiskProfile",
    "ChatThread",
    "McpToken",
    "encrypt_value",
    "decrypt_value",
    "hash_password",
    "verify_password",
    "needs_rehash",
    "MIN_PASSWORD_CHARS",
]
