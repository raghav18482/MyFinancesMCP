"""Writing the append-only decision record.

Every trade decision is recorded with the inputs it was made on, the model and
ruleset versions in force, and the outcome — including decisions that were
blocked, which are the ones an audit actually asks about.

The write is best-effort by design: a database problem must not stop or reverse
a trade that has already been placed with the broker. A failure is logged
loudly rather than raised, because the alternative is worse.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def payload_hash(payload: Any) -> str:
    """Stable hash of the decision inputs.

    ``sort_keys`` matters: without it the same payload hashes differently
    depending on dict ordering, and the hash stops being evidence of anything.
    """
    try:
        blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def record(
    *,
    proposal_id: str,
    symbol: str,
    side: str,
    quantity: float,
    outcome: str,
    price: Optional[float] = None,
    session_ref: Optional[str] = None,
    user_id: Optional[int] = None,
    approved_by: Optional[str] = None,
    order_id: Optional[str] = None,
    payload: Optional[dict] = None,
    model_version: Optional[str] = None,
    feature_set_version: Optional[int] = None,
    model_scores: Optional[dict] = None,
    ruleset_id: Optional[str] = None,
    compliance: Optional[dict] = None,
    rationale: Optional[str] = None,
) -> Optional[int]:
    """Append one decision record. Returns its id, or None if it could not be written."""
    try:
        from db.engine import get_session
        from db.models import DecisionRecord

        row = DecisionRecord(
            user_id=user_id,
            session_ref=_short(session_ref),
            proposal_id=proposal_id,
            symbol=str(symbol).upper(),
            side=str(side).upper(),
            quantity=float(quantity or 0),
            price=float(price) if price is not None else None,
            outcome=outcome,
            approved_by=approved_by,
            order_id=order_id,
            payload_hash=payload_hash(payload) if payload is not None else None,
            payload_json=_dump(payload),
            model_version=model_version,
            feature_set_version=feature_set_version,
            model_scores_json=_dump(model_scores),
            ruleset_id=ruleset_id,
            compliance_json=_dump(compliance),
            rationale=rationale,
        )

        with get_session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            logger.info("decision record %s: %s %s %s -> %s",
                        row.id, side, quantity, symbol, outcome)
            return row.id

    except Exception as e:
        # Deliberately swallowed. An order may already be live at the broker;
        # raising here would not un-place it, and would turn a bookkeeping
        # problem into a user-visible failure.
        logger.error("decision record NOT written for proposal %s (%s): %s",
                     proposal_id, outcome, e)
        return None


def recent(limit: int = 50, *, symbol: Optional[str] = None) -> list[dict]:
    """Read back recent decisions, newest first. For the audit view."""
    try:
        from sqlmodel import select

        from db.engine import get_session
        from db.models import DecisionRecord

        with get_session() as session:
            stmt = select(DecisionRecord).order_by(DecisionRecord.created_at.desc())
            if symbol:
                stmt = stmt.where(DecisionRecord.symbol == str(symbol).upper())
            rows = session.exec(stmt.limit(limit)).all()

        return [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "proposal_id": r.proposal_id,
                "symbol": r.symbol,
                "side": r.side,
                "quantity": r.quantity,
                "price": r.price,
                "outcome": r.outcome,
                "order_id": r.order_id,
                "model_version": r.model_version,
                "ruleset_id": r.ruleset_id,
                "payload_hash": r.payload_hash,
                "compliance": _load(r.compliance_json),
                "rationale": r.rationale,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("decision record read failed: %s", e)
        return []


def _dump(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except Exception:
        return None


def _load(blob: Optional[str]) -> Any:
    if not blob:
        return None
    try:
        return json.loads(blob)
    except Exception:
        return None


def _short(session_ref: Optional[str]) -> Optional[str]:
    """Store only a prefix of the session id — enough to correlate, not to reuse."""
    if not session_ref:
        return None
    return str(session_ref)[:12]
