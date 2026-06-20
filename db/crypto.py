"""Symmetric encryption helpers for sensitive user fields.

The Fernet key lives in the ``ENCRYPTION_KEY`` env var. Generate one with::

    python -m db.crypto generate

Only ``encrypt_value`` and ``decrypt_value`` are part of the public API.
Decrypted values must never be logged.
"""
from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    key = (os.environ.get("ENCRYPTION_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY missing. Generate one with: python -m db.crypto generate"
        )
    return Fernet(key.encode("ascii"))


def encrypt_value(raw: str) -> str:
    """Encrypt a plaintext string and return Fernet base64 ciphertext."""
    if raw is None:
        raise ValueError("encrypt_value: raw is None")
    return _fernet().encrypt(raw.encode("utf-8")).decode("ascii")


def decrypt_value(enc: str) -> str:
    """Reverse of ``encrypt_value``. Raises ``cryptography.fernet.InvalidToken`` on bad input."""
    if enc is None:
        raise ValueError("decrypt_value: enc is None")
    return _fernet().decrypt(enc.encode("ascii")).decode("utf-8")


def sign_token(payload: dict) -> str:
    """Serialise ``payload`` to JSON and return a Fernet token (signed + timestamped).

    The token embeds a creation timestamp so :func:`unsign_token` can enforce a
    max age. The token is opaque to the client and cannot be forged or mutated
    without ``ENCRYPTION_KEY``.
    """
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def unsign_token(token: str, max_age: int) -> dict:
    """Reverse of :func:`sign_token`, rejecting tokens older than ``max_age`` seconds.

    Raises ``cryptography.fernet.InvalidToken`` if the token is tampered with or
    expired, and ``ValueError`` if the decoded payload is not a JSON object.
    """
    if not token:
        raise ValueError("unsign_token: token is empty")
    raw = _fernet().decrypt(token.encode("ascii"), ttl=max_age)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("unsign_token: payload is not an object")
    return payload


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        print(Fernet.generate_key().decode("ascii"))
    else:
        print("usage: python -m db.crypto generate", file=sys.stderr)
        sys.exit(1)
