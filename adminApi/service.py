"""User-record operations, independent of any transport.

These functions own the *only* code path that writes Angel credentials to the
``users`` table: encrypt the secrets, enforce the ``angel_client_id`` uniqueness
rule, and return a secret-free view of the row.

They deliberately know nothing about HTTP. Both callers use them in-process:

- :mod:`adminApi.router` wraps them behind the ``X-Admin-Key`` header.
- ``POST /api/premium/register`` in :mod:`web.routers.premium` calls them for the
  already-authenticated browser session.

The second caller is the reason this module exists. It used to register users by
having the server POST to its *own* ``/api/admin/users`` endpoint over HTTP,
building the target URL from ``request.base_url`` — i.e. from the client's
``Host`` header. A request carrying ``Host: attacker.tld`` made the server ship
that user's plaintext PIN, TOTP secret and the admin key to the attacker.
Credentials must never leave the process to be stored; call these functions.

This module also owns the matching *read* path — :func:`credentials_for_user`,
which decrypts the stored secrets so a broker session can be rebuilt without the
user retyping them. It is the only function here that returns plaintext
secrets; everything else returns :func:`public_view` or a ``User``. Keep it
that way, and never log its return value.

Signing in to the app (:func:`authenticate_app_user`) and connecting a broker
(:func:`connect_brokerage`) are separate: a user can exist with a phone number
and password alone, ``brokerage_status`` ``not_connected`` and every Angel One
column NULL.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from cryptography.fernet import InvalidToken
from sqlalchemy.exc import IntegrityError
from sqlmodel import select, update

from db import (
    decrypt_value,
    encrypt_value,
    get_session,
    hash_password,
    needs_rehash,
    verify_password,
)
from db.models import BROKERAGE_PROVIDER, BrokerageStatus, User, utcnow

logger = logging.getLogger(__name__)


class UserServiceError(Exception):
    """Base class for expected, caller-handled user-record failures."""


class UserAlreadyExists(UserServiceError):
    """An active row already exists for this ``angel_client_id``."""

    def __init__(self, angel_client_id: str, user_id: int) -> None:
        super().__init__(
            f"User with angel_client_id '{angel_client_id}' already exists (id={user_id})."
        )
        self.angel_client_id = angel_client_id
        self.user_id = user_id


class WhatsappNumberTaken(UserServiceError):
    """A row already uses this ``whatsapp_number``, the app login identifier."""

    def __init__(self) -> None:
        super().__init__("That WhatsApp number is already registered.")


class BrokerAccountInUse(UserServiceError):
    """This Angel One client id is already linked to a different user."""

    def __init__(self) -> None:
        super().__init__("That Angel One account is already linked to another user.")


class UserNotFound(UserServiceError):
    """No ``users`` row with this id."""

    def __init__(self, user_id: int) -> None:
        super().__init__(f"User {user_id} not found.")
        self.user_id = user_id


def is_connected(user: User | None) -> bool:
    """True only for a user whose broker link is usable right now."""
    return bool(user) and user.brokerage_status == BrokerageStatus.CONNECTED


def available_features(user: User | None) -> list[str]:
    """What the user may open: learning always, brokerage once connected."""
    return ["learning", "brokerage"] if is_connected(user) else ["learning"]


def brokerage_provider(user: User | None) -> str | None:
    """The broker a user is (or was until it expired) linked to, else None."""
    if user and user.brokerage_status in (BrokerageStatus.CONNECTED, BrokerageStatus.EXPIRED):
        return BROKERAGE_PROVIDER
    return None


def public_view(user: User) -> dict[str, Any]:
    """Secret-free projection of a ``users`` row.

    Never add ``angel_api_key``, ``angel_password_encrypted``,
    ``angel_totp_secret_encrypted`` or ``angel_access_token`` here — every
    caller returns this straight to a client.
    """
    return {
        "id": user.id,
        "whatsapp_number": user.whatsapp_number,
        "angel_client_id": user.angel_client_id,
        "brokerage_status": user.brokerage_status,
        "brokerage_connected": is_connected(user),
        "available_features": available_features(user),
        "is_active": user.is_active,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def jsonable(view: dict[str, Any]) -> dict[str, Any]:
    """``public_view`` with datetimes ISO-formatted, for ``JSONResponse``."""
    return {
        k: (v.isoformat() if isinstance(v, datetime) else v)
        for k, v in view.items()
    }


def find_by_client_id(angel_client_id: str) -> Optional[dict[str, Any]]:
    """Return the secret-free view for an ``angel_client_id``, or ``None``."""
    with get_session() as session:
        user = session.exec(
            select(User).where(User.angel_client_id == angel_client_id.strip())
        ).first()
        return public_view(user) if user else None


def find_by_whatsapp_number(whatsapp_number: str) -> Optional[dict[str, Any]]:
    """Return the secret-free view for a ``whatsapp_number``, or ``None``.

    ``whatsapp_number`` is the login identifier, and the column is unique, so
    enrollment uses this to reject a number already claimed by another client id
    rather than letting the insert fail on the constraint.
    """
    with get_session() as session:
        user = session.exec(
            select(User).where(User.whatsapp_number == whatsapp_number.strip())
        ).first()
        return public_view(user) if user else None


def get_user(user_id: int) -> dict[str, Any]:
    """Return the secret-free view for ``user_id``. Raises :class:`UserNotFound`."""
    with get_session() as session:
        user = session.get(User, user_id)
        if not user:
            raise UserNotFound(user_id)
        return public_view(user)


def list_users() -> list[dict[str, Any]]:
    """Every registered user, secret-free."""
    with get_session() as session:
        return [public_view(u) for u in session.exec(select(User)).all()]


def create_user(
    *,
    whatsapp_number: str,
    angel_api_key: str,
    angel_client_id: str,
    angel_password: str,
    angel_totp_secret: str,
    app_password: str | None = None,
) -> dict[str, Any]:
    """Create a user, encrypting the PIN and TOTP secret at rest.

    ``app_password`` is hashed, not encrypted (see :mod:`db.passwords`), and is
    what the owner later types at ``/login``. Omitting it creates a row that
    cannot be logged into — which is what premium registration does, since that
    caller already has an authenticated session and asks for no password.

    The row is ``connected``: callers have either just proved the credentials
    with Angel One (enrollment) or are an operator registering them (admin API).

    Raises :class:`UserAlreadyExists` if the ``angel_client_id`` is taken.
    """
    client_id = angel_client_id.strip()
    with get_session() as session:
        existing = session.exec(
            select(User).where(User.angel_client_id == client_id)
        ).first()
        if existing:
            raise UserAlreadyExists(client_id, existing.id)

        user = User(
            whatsapp_number=whatsapp_number.strip(),
            angel_api_key=encrypt_value(angel_api_key.strip()),
            angel_client_id=client_id,
            angel_password_encrypted=encrypt_value(angel_password),
            angel_totp_secret_encrypted=encrypt_value(angel_totp_secret),
            app_password_hash=hash_password(app_password) if app_password else None,
            brokerage_status=BrokerageStatus.CONNECTED.value,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info("user created: id=%s client_id=%s", user.id, user.angel_client_id)
        return public_view(user)


def update_user(
    user_id: int,
    *,
    whatsapp_number: str | None = None,
    angel_api_key: str | None = None,
    angel_password: str | None = None,
    angel_totp_secret: str | None = None,
    app_password: str | None = None,
    is_active: bool | None = None,
    brokerage_status: BrokerageStatus | None = None,
) -> dict[str, Any]:
    """Patch a user in place; ``None`` means "leave unchanged".

    Secrets are re-encrypted when supplied, and ``app_password`` is re-hashed.
    ``brokerage_status`` is never inferred from the fields: a caller that has
    just proved the credentials says so by passing ``CONNECTED``.
    Raises :class:`UserNotFound`.
    """
    with get_session() as session:
        user = session.get(User, user_id)
        if not user:
            raise UserNotFound(user_id)

        if whatsapp_number is not None:
            user.whatsapp_number = whatsapp_number.strip()
        if angel_api_key is not None:
            user.angel_api_key = encrypt_value(angel_api_key.strip())
        if angel_password is not None:
            user.angel_password_encrypted = encrypt_value(angel_password)
        if angel_totp_secret is not None:
            user.angel_totp_secret_encrypted = encrypt_value(angel_totp_secret)
        if app_password is not None:
            user.app_password_hash = hash_password(app_password)
        if is_active is not None:
            user.is_active = is_active
        if brokerage_status is not None:
            user.brokerage_status = BrokerageStatus(brokerage_status).value

        user.updated_at = utcnow()
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info("user updated: id=%s", user.id)
        return public_view(user)


def deactivate_user(user_id: int) -> dict[str, Any]:
    """Soft-delete: flip ``is_active`` off, keeping the row for log FK integrity."""
    view = update_user(user_id, is_active=False)
    logger.info("user deactivated: id=%s", user_id)
    return view


# ── App accounts: phone number + password, no broker ────────────────────────


def create_app_user(*, whatsapp_number: str, app_password: str) -> User:
    """Create a user who can sign in but has not connected a broker.

    Every Angel One column is NULL and ``brokerage_status`` is
    ``not_connected``. Raises :class:`WhatsappNumberTaken` if the number is
    already registered (including by a race that loses on the unique index),
    and ``ValueError`` from :func:`db.passwords.hash_password` for a short
    password.
    """
    number = whatsapp_number.strip()
    password_hash = hash_password(app_password)
    with get_session() as session:
        if session.exec(select(User).where(User.whatsapp_number == number)).first():
            raise WhatsappNumberTaken()
        user = User(
            whatsapp_number=number,
            app_password_hash=password_hash,
            brokerage_status=BrokerageStatus.NOT_CONNECTED.value,
        )
        session.add(user)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise WhatsappNumberTaken() from None
        session.refresh(user)
        logger.info("app user created: id=%s (no broker)", user.id)
        return user


def authenticate_app_user(whatsapp_number: str, app_password: str) -> Optional[User]:
    """Verify an app password and return the active user, or ``None``.

    Nothing about the broker is read or decrypted: signing in to the app works
    the same whether or not a broker is connected.

    Returns ``None`` for every kind of failure — unknown number, inactive row,
    no app password set, wrong password — because telling them apart would leak
    which numbers are registered. :func:`db.passwords.verify_password` is called
    even when no row matches so the three failures take the same time.

    A successful login whose hash was made with outdated scrypt parameters is
    transparently upgraded, so raising the cost constants is a no-op for users.
    """
    number = (whatsapp_number or "").strip()
    with get_session() as session:
        user = (
            session.exec(select(User).where(User.whatsapp_number == number)).first()
            if number
            else None
        )

        stored = user.app_password_hash if user else None
        if not verify_password(app_password or "", stored):
            logger.info(
                "app login rejected for %s (known=%s, password_set=%s)",
                _mask(number),
                user is not None,
                bool(stored),
            )
            return None

        if not user.is_active:
            logger.info("app login rejected: row inactive for %s", _mask(number))
            return None

        if needs_rehash(stored):
            user.app_password_hash = hash_password(app_password)
            user.updated_at = utcnow()
            session.add(user)
            session.commit()
            logger.info("app password hash upgraded for user id=%s", user.id)

        session.refresh(user)
        logger.info("app login accepted for user id=%s", user.id)
        return user


# ── Brokerage link: written only after the broker accepted the credentials ──


def connect_brokerage(
    user_id: int,
    *,
    api_key: str,
    client_id: str,
    password: str,
    totp_secret: str,
) -> User:
    """Store proven Angel One credentials on ``user_id`` and mark it connected.

    Call only after a broker login with exactly these values succeeded. One
    transaction: either every credential column and the status change, or
    nothing does — a failure never leaves a half-written link, and never
    touches a previous link's values.

    Raises :class:`BrokerAccountInUse` if another row holds ``client_id`` and
    :class:`UserNotFound` if ``user_id`` is gone or inactive.
    """
    client_id = client_id.strip()
    with get_session() as session:
        user = session.get(User, user_id)
        if not user or not user.is_active:
            raise UserNotFound(user_id)
        owner = session.exec(
            select(User).where(User.angel_client_id == client_id, User.id != user_id)
        ).first()
        if owner:
            raise BrokerAccountInUse()

        user.angel_api_key = encrypt_value(api_key.strip())
        user.angel_client_id = client_id
        user.angel_password_encrypted = encrypt_value(password)
        user.angel_totp_secret_encrypted = encrypt_value(totp_secret.strip())
        user.brokerage_status = BrokerageStatus.CONNECTED.value
        user.updated_at = utcnow()
        session.add(user)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise BrokerAccountInUse() from None
        session.refresh(user)
        logger.info("brokerage connected for user id=%s", user.id)
        return user


def set_brokerage_status(
    user_id: int,
    status: BrokerageStatus,
    *,
    only_if: tuple[BrokerageStatus, ...] | None = None,
) -> bool:
    """Set ``brokerage_status``, optionally only from one of ``only_if``.

    A single conditional ``UPDATE``, so two requests racing (say, a rebuild
    marking a link expired while the user reconnects) cannot undo each other's
    result. Credentials are never touched. Returns whether a row changed.
    """
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(brokerage_status=BrokerageStatus(status).value, updated_at=utcnow())
    )
    if only_if:
        stmt = stmt.where(User.brokerage_status.in_([BrokerageStatus(s).value for s in only_if]))
    with get_session() as session:
        changed = session.exec(stmt).rowcount
        session.commit()
    if changed:
        logger.info("brokerage status for user id=%s -> %s", user_id, BrokerageStatus(status).value)
    return bool(changed)


def disconnect_brokerage(user_id: int) -> User:
    """Unlink the broker: wipe every stored credential, mark ``disconnected``.

    Wiping (rather than keeping the ciphertext) means nothing can rebuild a
    session for this account afterwards, and frees the client id so it can be
    linked again — by this user or, after they hand it over, another.
    Raises :class:`UserNotFound`.
    """
    with get_session() as session:
        user = session.get(User, user_id)
        if not user:
            raise UserNotFound(user_id)
        user.angel_api_key = None
        user.angel_client_id = None
        user.angel_password_encrypted = None
        user.angel_totp_secret_encrypted = None
        user.angel_access_token = None
        user.brokerage_status = BrokerageStatus.DISCONNECTED.value
        user.updated_at = utcnow()
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info("brokerage disconnected for user id=%s", user.id)
        return user


# ── The read path: decrypting credentials to rebuild a broker session ──────
#
# Everything below returns plaintext broker secrets. Callers hand them straight
# to ``session_manager.sessions.create_session`` and keep no other copy. Never
# log a return value, and never put these behind an HTTP response.


@dataclass(frozen=True)
class AngelCredentials:
    """The four values ``AngelOneClient`` needs, plus the row they came from."""

    user_id: int
    # ``repr=False`` so an accidental log of this object never prints a secret.
    api_key: str = field(repr=False)
    client_id: str
    password: str = field(repr=False)
    totp_secret: str = field(repr=False)


def _read_api_key(stored: str) -> str:
    """The API key, whichever way the row stores it.

    Rows written by this release hold a Fernet token; rows written before hold
    the key in plaintext. An Angel One API key is never a valid Fernet token,
    so a value that does not decrypt is the legacy plaintext form.
    """
    try:
        return decrypt_value(stored)
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return stored


def _decrypt_row(user: User) -> AngelCredentials:
    return AngelCredentials(
        user_id=user.id,
        api_key=_read_api_key(user.angel_api_key),
        client_id=user.angel_client_id,
        password=decrypt_value(user.angel_password_encrypted),
        totp_secret=decrypt_value(user.angel_totp_secret_encrypted),
    )


def credentials_for_user(user_id: int) -> Optional[AngelCredentials]:
    """Decrypted credentials for an already-authenticated, connected user.

    Used to rebuild an expired in-memory broker session for an identity the
    server has already verified: a signed cookie whose session version still
    matches, or a verified MCP bearer token. No password is checked here, so
    never call this with an id taken from request parameters or tool arguments.

    ``None`` unless the row is active and ``connected``: an expired or
    disconnected link must not be used, even if ciphertext is still stored.
    """
    with get_session() as session:
        user = session.get(User, user_id)
        if not user or not user.is_active or not is_connected(user):
            return None
        if not (user.angel_client_id and user.angel_password_encrypted
                and user.angel_totp_secret_encrypted and user.angel_api_key):
            return None
        return _decrypt_row(user)


@dataclass(frozen=True)
class SessionState:
    """What a request needs to know to trust an existing session for ``user_id``."""

    user_id: int
    is_active: bool
    session_version: int
    angel_client_id: Optional[str]
    brokerage_status: str


def session_state(user_id: int) -> Optional[SessionState]:
    """Current activation, session version and broker link for a user, or None."""
    with get_session() as session:
        user = session.get(User, user_id)
        if not user:
            return None
        return SessionState(
            user_id=user.id,
            is_active=bool(user.is_active),
            session_version=int(user.session_version or 0),
            angel_client_id=user.angel_client_id,
            brokerage_status=user.brokerage_status,
        )


def bump_session_version(user_id: int) -> Optional[int]:
    """Invalidate every cookie issued to ``user_id``; return the new version."""
    with get_session() as session:
        user = session.get(User, user_id)
        if not user:
            return None
        user.session_version = int(user.session_version or 0) + 1
        user.updated_at = utcnow()
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info("session version bumped for user id=%s", user_id)
        return user.session_version


def _mask(whatsapp_number: str) -> str:
    """Last 4 digits only, so logs can identify a login attempt but not a person."""
    return f"***{whatsapp_number[-4:]}" if len(whatsapp_number) > 4 else "***"
