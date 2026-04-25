"""Symmetric encryption helpers for sensitive user fields.

The Fernet key lives in the ``ENCRYPTION_KEY`` env var. Generate one with::

    python -m db.crypto generate

Only ``encrypt_value`` and ``decrypt_value`` are part of the public API.
Decrypted values must never be logged.
"""
from __future__ import annotations

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


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        print(Fernet.generate_key().decode("ascii"))
    else:
        print("usage: python -m db.crypto generate", file=sys.stderr)
        sys.exit(1)
