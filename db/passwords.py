"""One-way hashing for the app's own login password.

Why this is not :mod:`db.crypto`
--------------------------------
``db.crypto`` is *reversible* Fernet encryption, and that is the right tool for
the Angel One PIN and TOTP secret: the server genuinely has to recover the
plaintext to hand it to the broker's API.

The app password is the opposite case. The server never needs to read it back —
at login it only has to answer "does what was just typed match what was
stored?". So it is hashed, never encrypted. Keeping it one-way means a leaked
``ENCRYPTION_KEY`` cannot be turned into a list of readable user passwords.

``hashlib.scrypt`` (RFC 7914) is used rather than argon2 or bcrypt because it is
memory-hard, salted per password, and already in the standard library — this
project pins every dependency, so not adding one has value. A fast hash such as
SHA-256 would be wrong here no matter how it is salted: the whole point is to be
slow enough that guessing is impractical.

Stored format is self-describing so the cost parameters can be raised later
without invalidating existing hashes::

    scrypt$<n>$<r>$<p>$<salt_b64>$<key_b64>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os

logger = logging.getLogger(__name__)

_SCHEME = "scrypt"

# Interactive-login cost: 128 * n * r bytes ≈ 16 MiB and ~100 ms per check.
# Negligible for one login, ruinous for an offline brute-force.
_N = 2 ** 14
_R = 8
_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_MAXMEM = 64 * 1024 * 1024  # headroom over the 16 MiB above; OpenSSL defaults vary

#: Shortest password accepted at enrollment. One password now unlocks a live
#: trading account, so this is deliberately longer than a 4-digit PIN.
MIN_PASSWORD_CHARS = 10

# Precomputed once, used to spend the same time on a miss as on a hit.
_dummy_hash: str | None = None


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(raw: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        raw.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_KEY_BYTES,
        maxmem=_MAXMEM,
    )


def hash_password(raw: str) -> str:
    """Return a self-describing scrypt hash of ``raw``.

    Raises ``ValueError`` if the password is empty or shorter than
    :data:`MIN_PASSWORD_CHARS`.
    """
    if raw is None:
        raise ValueError("hash_password: raw is None")
    if len(raw) < MIN_PASSWORD_CHARS:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_CHARS} characters."
        )
    salt = os.urandom(_SALT_BYTES)
    key = _derive(raw, salt, _N, _R, _P)
    return f"{_SCHEME}${_N}${_R}${_P}${_b64(salt)}${_b64(key)}"


def verify_password(raw: str, stored: str | None) -> bool:
    """Check ``raw`` against a hash from :func:`hash_password`.

    ``stored`` may be ``None`` or empty — for a user who does not exist, or one
    enrolled before app passwords existed. Those cases still spend the full
    hashing time before returning ``False``, so response timing does not reveal
    which WhatsApp numbers are registered.
    """
    if not stored or not raw:
        _burn()
        return False

    try:
        scheme, n_s, r_s, p_s, salt_b64, key_b64 = stored.split("$")
        if scheme != _SCHEME:
            raise ValueError(f"unknown hash scheme: {scheme!r}")
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(key_b64.encode("ascii"))
    except Exception:
        # A malformed hash is a bug or a corrupted row, never a valid login.
        logger.exception("verify_password: could not parse stored hash")
        _burn()
        return False

    candidate = _derive(raw, salt, n, r, p)
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str | None) -> bool:
    """True if ``stored`` was made with weaker parameters than the current ones.

    Call after a successful :func:`verify_password` to transparently upgrade a
    hash on login once the costs above are raised.
    """
    if not stored:
        return False
    try:
        scheme, n_s, r_s, p_s, _salt, _key = stored.split("$")
    except ValueError:
        return False
    return scheme != _SCHEME or (int(n_s), int(r_s), int(p_s)) != (_N, _R, _P)


def _burn() -> None:
    """Spend one hash's worth of time to flatten the timing of a failed login."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(base64.b64encode(os.urandom(24)).decode("ascii"))
    verify_password("x" * MIN_PASSWORD_CHARS, _dummy_hash)
