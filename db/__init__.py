"""Persistence layer: SQLModel + Postgres + Fernet-encrypted credentials.

Public surface kept tiny on purpose:
- ``engine`` / ``get_session`` / ``init_db``
- model classes (``User``, ``Schedule``, ``Log``)
- ``encrypt_value`` / ``decrypt_value`` — reversible, for broker credentials
- ``hash_password`` / ``verify_password`` — one-way, for the app's own login
"""
from __future__ import annotations

from .crypto import decrypt_value, encrypt_value, sign_token, unsign_token
from .engine import DATABASE_URL, engine, get_session, init_db
from .models import ChatThread, Log, RiskProfile, Schedule, User
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
    "encrypt_value",
    "decrypt_value",
    "sign_token",
    "unsign_token",
    "hash_password",
    "verify_password",
    "needs_rehash",
    "MIN_PASSWORD_CHARS",
]
