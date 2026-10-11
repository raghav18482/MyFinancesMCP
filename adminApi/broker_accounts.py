"""Persistence operations for linked broker accounts."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import select

from db import decrypt_value, encrypt_value, get_session
from db.models import BrokerAccount, User, utcnow


def save_zerodha_account(user_id: int, profile: dict, access_token: str) -> int:
    broker_user_id = str(profile.get("user_id") or "").strip()
    if not broker_user_id:
        raise ValueError("Zerodha did not return an account identifier")
    label = str(profile.get("user_name") or profile.get("user_shortname") or broker_user_id)[:120]
    with get_session() as session:
        user = session.get(User, user_id)
        if not user or not user.is_active:
            raise ValueError("App account is no longer available")
        account = session.exec(
            select(BrokerAccount).where(
                BrokerAccount.broker == "zerodha",
                BrokerAccount.broker_user_id == broker_user_id,
            )
        ).first()
        if account and account.user_id != user_id:
            raise ValueError("This Zerodha account is already linked to another app account")
        expiry = datetime.now(timezone.utc).replace(hour=0, minute=30, second=0, microsecond=0)
        if expiry <= datetime.now(timezone.utc):
            from datetime import timedelta
            expiry += timedelta(days=1)
        if account is None:
            account = BrokerAccount(
                user_id=user_id, broker="zerodha", broker_user_id=broker_user_id,
                display_name=label, access_token_encrypted=encrypt_value(access_token),
                token_expires_at=expiry,
            )
        else:
            account.display_name = label
            account.access_token_encrypted = encrypt_value(access_token)
            account.token_expires_at = expiry
            account.is_active = True
            account.updated_at = utcnow()
        session.add(account)
        session.commit()
        session.refresh(account)
        return int(account.id)


def account_credentials(user_id: int, account_id: int) -> tuple[str, str, str] | None:
    with get_session() as session:
        account = session.exec(
            select(BrokerAccount).join(User, User.id == BrokerAccount.user_id).where(
                BrokerAccount.id == account_id,
                BrokerAccount.user_id == user_id,
                User.is_active == True,  # noqa: E712
                BrokerAccount.broker == "zerodha",
                BrokerAccount.is_active == True,  # noqa: E712
            )
        ).first()
        if account is None:
            return None
        from os import environ
        api_key = (environ.get("KITE_API_KEY") or "").strip()
        if not api_key:
            return None
        return api_key, decrypt_value(account.access_token_encrypted), account.broker_user_id, account.token_expires_at


def list_accounts(user_id: int) -> list[dict]:
    with get_session() as session:
        return [
            {"id": row.id, "broker": row.broker, "broker_user_id": row.broker_user_id,
             "display_name": row.display_name, "token_expires_at": row.token_expires_at.isoformat(),
             "is_active": row.is_active}
            for row in session.exec(select(BrokerAccount).where(
                BrokerAccount.user_id == user_id, BrokerAccount.is_active == True  # noqa: E712
            )).all()
        ]
