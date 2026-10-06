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
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlmodel import select

from db import encrypt_value, get_session
from db.models import User, utcnow

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


class UserNotFound(UserServiceError):
    """No ``users`` row with this id."""

    def __init__(self, user_id: int) -> None:
        super().__init__(f"User {user_id} not found.")
        self.user_id = user_id


def public_view(user: User) -> dict[str, Any]:
    """Secret-free projection of a ``users`` row.

    Never add ``angel_password_encrypted``, ``angel_totp_secret_encrypted`` or
    ``angel_access_token`` here — every caller returns this straight to a client.
    """
    return {
        "id": user.id,
        "whatsapp_number": user.whatsapp_number,
        "angel_api_key": user.angel_api_key,
        "angel_client_id": user.angel_client_id,
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
) -> dict[str, Any]:
    """Create a user, encrypting the PIN and TOTP secret at rest.

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
            angel_api_key=angel_api_key.strip(),
            angel_client_id=client_id,
            angel_password_encrypted=encrypt_value(angel_password),
            angel_totp_secret_encrypted=encrypt_value(angel_totp_secret),
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
    is_active: bool | None = None,
) -> dict[str, Any]:
    """Patch a user in place; ``None`` means "leave unchanged".

    Secrets are re-encrypted when supplied. Raises :class:`UserNotFound`.
    """
    with get_session() as session:
        user = session.get(User, user_id)
        if not user:
            raise UserNotFound(user_id)

        if whatsapp_number is not None:
            user.whatsapp_number = whatsapp_number.strip()
        if angel_api_key is not None:
            user.angel_api_key = angel_api_key.strip()
        if angel_password is not None:
            user.angel_password_encrypted = encrypt_value(angel_password)
        if angel_totp_secret is not None:
            user.angel_totp_secret_encrypted = encrypt_value(angel_totp_secret)
        if is_active is not None:
            user.is_active = is_active

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
