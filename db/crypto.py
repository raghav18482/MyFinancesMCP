"""Encryption for stored secrets, and sealed short-lived tokens. Two key rings.

Both use Fernet: AES-128-CBC with an HMAC-SHA256 and an embedded timestamp,
i.e. authenticated encryption. Tokens are therefore *sealed* and *opened*,
not signed: the client cannot read or alter them.

**Data keys** encrypt broker credentials at rest.

- ``ENCRYPTION_KEY`` is data key version 1. Every existing ciphertext is v1.
- ``DATA_ENCRYPTION_KEYS="2:<fernet>,3:<fernet>"`` optionally adds versions 2
  and up (version 1 is never listed there), and ``DATA_ENCRYPTION_PRIMARY``
  picks the one :func:`encrypt_value_versioned` uses (default 1).
- :func:`decrypt_value` uses exactly the version it is told; there is no trial
  decryption across keys.

**Token keys** seal the feedback form token and the pending OAuth consent
request, and nothing else (a test enforces it).

- ``TOKEN_KEYS="t1:<fernet>"``, with ``TOKEN_PRIMARY`` naming the key to seal
  with (optional when there is one key). They are independent of the data
  keys; production refuses to start if one equals a data key.
- A token is ``<kid>.<fernet>``; its payload carries ``aud``.
  :func:`open_token` looks the kid up exactly and checks the audience and age.
- Outside production with ``TOKEN_KEYS`` unset, a random key is made per
  process (tokens then last until a restart, at most ten minutes anyway).

Tokens without a kid (the format before R8, sealed with ``ENCRYPTION_KEY``)
are refused since R9.

Decrypted values must never be logged. Generate a key with::

    python -m db.crypto generate
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_KID = re.compile(r"[a-z0-9]{1,16}")


class KeyConfigError(RuntimeError):
    """The key ring environment variables are missing or malformed."""


class TokenError(ValueError):
    """A token could not be opened: malformed, unknown key, tampered, expired, or
    for another audience. Deliberately one type with no detail for the client."""


# ── Data keys ─────────────────────────────────────────────────────────────


def _fernet_from(value: str, name: str) -> Fernet:
    try:
        return Fernet(value.strip().encode("ascii"))
    except Exception:
        raise KeyConfigError(f"{name} is not a valid Fernet key") from None


def _fernet() -> Fernet:
    """Data key version 1 (``ENCRYPTION_KEY``)."""
    key = (os.environ.get("ENCRYPTION_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY missing. Generate one with: python -m db.crypto generate"
        )
    return Fernet(key.encode("ascii"))


def _parse_ring(raw: str, name: str) -> dict[str, str]:
    ring: dict[str, str] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        ident, sep, key = item.partition(":")
        if not sep or not ident.strip() or not key.strip():
            raise KeyConfigError(f"{name} entries look like <id>:<fernet key>")
        ident = ident.strip()
        if ident in ring:
            raise KeyConfigError(f"{name} lists {ident!r} twice")
        ring[ident] = key.strip()
    return ring


def data_key_strings() -> dict[int, str]:
    """``{version: key}`` for every data key, version 1 first."""
    keys = {1: (os.environ.get("ENCRYPTION_KEY") or "").strip()}
    for ident, key in _parse_ring(os.environ.get("DATA_ENCRYPTION_KEYS", ""), "DATA_ENCRYPTION_KEYS").items():
        if not ident.isdigit() or int(ident) < 2:
            raise KeyConfigError("DATA_ENCRYPTION_KEYS lists versions 2 and up; version 1 is ENCRYPTION_KEY")
        keys[int(ident)] = key
    return keys


def data_keyring() -> dict[int, Fernet]:
    if not data_key_strings()[1]:
        _fernet()  # raises the usual "ENCRYPTION_KEY missing"
    return {v: _fernet_from(k, f"data key {v}") for v, k in data_key_strings().items()}


def data_primary() -> int:
    raw = (os.environ.get("DATA_ENCRYPTION_PRIMARY") or "1").strip()
    if not raw.isdigit() or int(raw) not in data_key_strings():
        raise KeyConfigError("DATA_ENCRYPTION_PRIMARY names no data key")
    return int(raw)


def encrypt_value(raw: str) -> str:
    """Encrypt with data key version 1, the version every legacy column holds."""
    if raw is None:
        raise ValueError("encrypt_value: raw is None")
    return _fernet().encrypt(raw.encode("utf-8")).decode("ascii")


def encrypt_value_versioned(raw: str) -> tuple[str, int]:
    """Encrypt with the primary data key; returns ``(ciphertext, version)``."""
    if raw is None:
        raise ValueError("encrypt_value_versioned: raw is None")
    version = data_primary()
    return data_keyring()[version].encrypt(raw.encode("utf-8")).decode("ascii"), version


def decrypt_value(enc: str, version: int = 1) -> str:
    """Decrypt with exactly data key ``version``. Raises ``InvalidToken`` otherwise."""
    if enc is None:
        raise ValueError("decrypt_value: enc is None")
    fernet = _fernet() if version == 1 else data_keyring().get(version)
    if fernet is None:
        raise InvalidToken
    return fernet.decrypt(enc.encode("ascii")).decode("utf-8")


# ── Token keys ────────────────────────────────────────────────────────────

_ephemeral_lock = threading.Lock()
_ephemeral: Optional[str] = None


def _ephemeral_key() -> str:
    global _ephemeral
    with _ephemeral_lock:
        if _ephemeral is None:
            _ephemeral = Fernet.generate_key().decode("ascii")
            logger.warning("TOKEN_KEYS is not set; using a per-process token key (development only)")
        return _ephemeral


def token_key_strings() -> dict[str, str]:
    """``{kid: key}`` from ``TOKEN_KEYS``; an ephemeral key outside production if unset."""
    ring = _parse_ring(os.environ.get("TOKEN_KEYS", ""), "TOKEN_KEYS")
    for kid in ring:
        if not _KID.fullmatch(kid):
            raise KeyConfigError("TOKEN_KEYS ids are 1-16 lower-case letters or digits")
    if ring:
        return ring
    from web.security_config import is_production

    if is_production():
        raise KeyConfigError("TOKEN_KEYS must be set in production")
    return {"dev": _ephemeral_key()}


def token_keyring() -> dict[str, Fernet]:
    return {kid: _fernet_from(k, f"token key {kid}") for kid, k in token_key_strings().items()}


def token_primary() -> str:
    ring = token_key_strings()
    primary = (os.environ.get("TOKEN_PRIMARY") or "").strip()
    if not primary:
        if len(ring) == 1:
            return next(iter(ring))
        raise KeyConfigError("TOKEN_PRIMARY must name one of several TOKEN_KEYS")
    if primary not in ring:
        raise KeyConfigError("TOKEN_PRIMARY names no key in TOKEN_KEYS")
    return primary


def seal_token(payload: dict, *, audience: str) -> str:
    """Seal ``payload`` for ``audience`` with the primary token key: ``<kid>.<fernet>``."""
    if not audience:
        raise ValueError("seal_token: audience is required")
    if "aud" in payload:
        raise ValueError("seal_token: payload must not carry its own aud")
    kid = token_primary()
    raw = json.dumps({**payload, "aud": audience}, separators=(",", ":")).encode("utf-8")
    return f"{kid}.{token_keyring()[kid].encrypt(raw).decode('ascii')}"


def _load_object(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise TokenError("bad token") from None
    if not isinstance(payload, dict):
        raise TokenError("bad token")
    return payload


def open_token(token: str, *, audience: str, max_age: int) -> dict:
    """Open a token sealed for ``audience`` no more than ``max_age`` seconds ago.

    Raises :class:`TokenError` for anything else, including a token without a
    kid (the format before R8).
    """
    if not token or not isinstance(token, str):
        raise TokenError("bad token")
    kid, sep, body = token.partition(".")
    if not sep:
        raise TokenError("bad token")
    fernet = token_keyring().get(kid)
    if fernet is None:
        raise TokenError("bad token")
    try:
        raw = fernet.decrypt(body.encode("ascii"), ttl=max_age)
    except (InvalidToken, UnicodeEncodeError, ValueError):
        raise TokenError("bad token") from None
    payload = _load_object(raw)
    if payload.get("aud") != audience:
        raise TokenError("bad token")
    return payload


def validate_key_config(production: bool) -> None:
    """Raise :class:`KeyConfigError` on key settings production must not start with."""
    data_keyring()  # parses and checks every data key
    data_primary()
    if not production:
        return
    tokens = token_key_strings()
    token_primary()
    token_keyring()
    if set(data_key_strings().values()) & set(tokens.values()):
        raise KeyConfigError("a token key must not also be a data key")
    if len(set(tokens.values())) != len(tokens):
        raise KeyConfigError("TOKEN_KEYS lists the same key twice")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        print(Fernet.generate_key().decode("ascii"))
    else:
        print("usage: python -m db.crypto generate", file=sys.stderr)
        sys.exit(1)
