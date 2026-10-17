"""Deleting a user's account, as the app stores require.

What goes: the ``users`` row (and with it the encrypted broker credentials and
the app password hash), saved chat threads and their ADK conversations,
briefing schedules and their logs, the risk profile, and every MCP personal
access token (so a token stops working the moment its account is gone).

What stays: ``decision_records``. That table is the append-only audit trail of
trade decisions — what an audit asks about — so rows are kept but their
``user_id`` is cleared, which leaves nothing that links them to a person. The
broker keeps its own records of placed orders regardless; deleting here never
touches the user's broker account.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlmodel import select

from db import get_session
from db.models import ChatThread, DecisionRecord, Log, McpToken, RiskProfile, Schedule, User
from db.passwords import verify_password

logger = logging.getLogger(__name__)


@dataclass
class DeletionReport:
    """What a deletion removed, for the log line and the tests."""

    user_id: int
    threads: list[tuple[str, str]] = field(default_factory=list)  # (agent_type, adk_session_id)
    schedules: int = 0
    logs: int = 0
    risk_profiles: int = 0
    decisions_unlinked: int = 0
    mcp_tokens: int = 0


def password_matches(user: User | None, app_password: str) -> bool:
    """Checks the app password, taking the same time whether or not there is
    a user or a hash, so a failure says nothing about which it was."""
    stored = user.app_password_hash if user else None
    return verify_password(app_password or "", stored)


def user_by_number(whatsapp_number: str) -> User | None:
    number = (whatsapp_number or "").strip()
    if not number:
        return None
    with get_session() as db:
        return db.exec(select(User).where(User.whatsapp_number == number)).first()


def delete_user(user_id: int) -> DeletionReport:
    """Removes everything stored for ``user_id`` in one transaction.

    ADK conversation rows live in another store and are removed afterwards,
    best effort, by :func:`delete_conversations`; a failure there leaves only
    orphaned turns keyed by an id nothing points to any more.
    """
    report = DeletionReport(user_id=user_id)
    with get_session() as db:
        for t in db.exec(select(ChatThread).where(ChatThread.user_id == user_id)).all():
            report.threads.append((t.agent_type, t.adk_session_id))
            db.delete(t)

        schedule_ids = []
        for s in db.exec(select(Schedule).where(Schedule.user_id == user_id)).all():
            schedule_ids.append(s.id)
            report.schedules += 1
        # Logs point at schedules, so they go first.
        for log in db.exec(select(Log).where(Log.user_id == user_id)).all():
            db.delete(log)
            report.logs += 1
        db.flush()
        for s in db.exec(select(Schedule).where(Schedule.user_id == user_id)).all():
            db.delete(s)

        for p in db.exec(select(RiskProfile).where(RiskProfile.user_id == user_id)).all():
            db.delete(p)
            report.risk_profiles += 1

        for tok in db.exec(select(McpToken).where(McpToken.user_id == user_id)).all():
            db.delete(tok)
            report.mcp_tokens += 1

        for d in db.exec(select(DecisionRecord).where(DecisionRecord.user_id == user_id)).all():
            d.user_id = None
            db.add(d)
            report.decisions_unlinked += 1

        db.flush()
        user = db.get(User, user_id)
        if user is not None:
            db.delete(user)
        db.commit()

    # Drop any live MCP broker session for this user right away.
    from services.mcp_tokens import mcp_session_key
    from session_manager import sessions

    sessions.remove_session(mcp_session_key(user_id))

    logger.info(
        "Deleted user %s: %d threads, %d schedules, %d logs, %d risk profiles, "
        "%d decision records unlinked, %d MCP tokens",
        user_id,
        len(report.threads),
        report.schedules,
        report.logs,
        report.risk_profiles,
        report.decisions_unlinked,
        report.mcp_tokens,
    )
    return report


async def delete_conversations(report: DeletionReport) -> None:
    """Best-effort removal of the ADK sessions behind the deleted threads."""
    if not report.threads:
        return
    from services.adk_runner_registry import app_name_for, registry

    for agent_type, adk_session_id in report.threads:
        await registry.delete_session(
            user_id=f"user-{report.user_id}",
            adk_session_id=adk_session_id,
            app_name=app_name_for(agent_type),
        )
