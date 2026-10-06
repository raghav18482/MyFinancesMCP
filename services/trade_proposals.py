"""
Trade proposal store with TTL-based expiry and server-only execution.

The LLM agent creates proposals; only trusted server code can approve and execute.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from services import compliance, decision_record
from services.broker_service import place_order_result
from services.risk_profile import ClientRiskProfile, risk_profiles

logger = logging.getLogger(__name__)

DEFAULT_TTL = 300  # 5 minutes


@dataclass
class TradeProposal:
    proposal_id: str
    session_id: str
    created_at: float
    ttl_seconds: int
    status: str  # pending | approved | rejected | expired | executed | failed

    order_params: dict
    summary: str

    order_id: Optional[str] = None
    error: Optional[str] = None

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "status": self.effective_status,
            "summary": self.summary,
            "order_params": self.order_params,
            "created_at": self.created_at,
            "ttl_seconds": self.ttl_seconds,
            "order_id": self.order_id,
            "error": self.error,
        }

    @property
    def effective_status(self) -> str:
        if self.status == "pending" and self.is_expired():
            return "expired"
        return self.status


class TradeProposalStore:
    def __init__(self) -> None:
        self._proposals: dict[str, TradeProposal] = {}
        self._lock = threading.Lock()

    def create(
        self,
        session_id: str,
        order_params: dict,
        summary: str,
        ttl: int = DEFAULT_TTL,
    ) -> TradeProposal:
        proposal_id = uuid.uuid4().hex[:12]
        proposal = TradeProposal(
            proposal_id=proposal_id,
            session_id=session_id,
            created_at=time.time(),
            ttl_seconds=ttl,
            status="pending",
            order_params=order_params,
            summary=summary,
        )
        with self._lock:
            self._proposals[proposal_id] = proposal
        logger.info(
            "Trade proposal %s created for session %s: %s",
            proposal_id, session_id[:8], summary,
        )
        return proposal

    def get(self, proposal_id: str) -> Optional[TradeProposal]:
        with self._lock:
            return self._proposals.get(proposal_id)

    def list_for_session(self, session_id: str) -> list[TradeProposal]:
        with self._lock:
            return [
                p for p in self._proposals.values()
                if p.session_id == session_id
            ]

    def approve(self, session_id: str, proposal_id: str) -> TradeProposal:
        proposal = self._get_owned(session_id, proposal_id)
        if proposal.effective_status != "pending":
            raise ValueError(f"Proposal {proposal_id} is {proposal.effective_status}, cannot approve")
        proposal.status = "approved"
        return proposal

    def reject(self, session_id: str, proposal_id: str) -> TradeProposal:
        proposal = self._get_owned(session_id, proposal_id)
        if proposal.effective_status != "pending":
            raise ValueError(f"Proposal {proposal_id} is {proposal.effective_status}, cannot reject")
        proposal.status = "rejected"
        return proposal

    def cleanup_expired(self) -> int:
        removed = 0
        with self._lock:
            expired_ids = [
                pid for pid, p in self._proposals.items()
                if p.effective_status == "expired" and (time.time() - p.created_at > p.ttl_seconds + 3600)
            ]
            for pid in expired_ids:
                del self._proposals[pid]
                removed += 1
        if removed:
            logger.info("Cleaned up %d old expired proposals", removed)
        return removed

    def _get_owned(self, session_id: str, proposal_id: str) -> TradeProposal:
        with self._lock:
            proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ValueError(f"Proposal {proposal_id} not found")
        if proposal.session_id != session_id:
            raise PermissionError(f"Proposal {proposal_id} does not belong to this session")
        return proposal


proposal_store = TradeProposalStore()


def validate_against_profile(order_params: dict, profile: ClientRiskProfile) -> None:
    """Raise ValueError if the order violates risk profile constraints."""
    product = order_params.get("producttype", "").upper()
    if product and product not in profile.allowed_products:
        raise ValueError(
            f"Product type '{product}' not allowed. Allowed: {profile.allowed_products}"
        )

    qty = int(order_params.get("quantity", 0))
    price = float(order_params.get("price", 0))
    if price == 0:
        price = float(order_params.get("triggerprice", 0))
    if price > 0 and qty > 0:
        order_value = qty * price
        if order_value > profile.max_single_order_value:
            raise ValueError(
                f"Order value {order_value:.0f} exceeds max allowed {profile.max_single_order_value:.0f}"
            )


def execute_proposal(
    session_id: str,
    proposal_id: str,
    client: Any,
    *,
    scheme: str = "default",
    portfolio: dict | None = None,
) -> dict[str, Any]:
    """
    Server-only execution path: ownership, status, risk profile, compliance, order.

    Two gates run before anything reaches the broker. The risk profile is the
    user's own stated appetite; compliance is the mandate, and it is a hard
    block that cites the rule it failed. Every outcome — executed, blocked or
    failed — is written to the append-only decision record, because a blocked
    trade is exactly what an audit asks about.
    """
    proposal = proposal_store._get_owned(session_id, proposal_id)
    params = proposal.order_params

    def _record(outcome: str, *, order_id: str | None = None,
                compliance: dict | None = None, rationale: str | None = None) -> None:
        decision_record.record(
            proposal_id=proposal_id,
            symbol=params.get("tradingsymbol") or params.get("symbol") or "",
            side=params.get("transactiontype", "BUY"),
            quantity=_as_float(params.get("quantity")),
            price=_as_float(params.get("price")) or None,
            outcome=outcome,
            session_ref=session_id,
            approved_by=session_id[:12] if session_id else None,
            order_id=order_id,
            payload=params,
            ruleset_id=(compliance or {}).get("ruleset_id"),
            compliance=compliance,
            rationale=rationale or proposal.summary,
        )

    if proposal.effective_status == "expired":
        proposal.status = "expired"
        _record("rejected", rationale="proposal expired before execution")
        return {"ok": False, "error": "Proposal has expired"}

    if proposal.status != "approved":
        return {"ok": False, "error": f"Proposal is {proposal.status}, must be approved first"}

    profile = risk_profiles.get(session_id)
    if profile:
        try:
            validate_against_profile(params, profile)
        except ValueError as e:
            proposal.status = "failed"
            proposal.error = str(e)
            _record("blocked", rationale=f"risk profile: {e}")
            return {"ok": False, "error": f"Risk check failed: {e}"}

    # Mandate and compliance gate. A breach blocks and names the rule.
    compliance_dict: dict | None = None
    try:
        result_c = compliance.check(params, portfolio=portfolio, scheme=scheme)
        compliance_dict = result_c.to_dict()
        for warning in result_c.warnings:
            logger.warning("Proposal %s compliance warning: %s", proposal_id,
                           warning.describe())
        if result_c.blocked:
            reason = result_c.reason()
            proposal.status = "failed"
            proposal.error = reason
            logger.warning("Proposal %s BLOCKED by compliance: %s", proposal_id, reason)
            _record("blocked", compliance=compliance_dict,
                    rationale=f"compliance: {reason}")
            return {"ok": False, "error": f"Compliance check failed: {reason}",
                    "compliance": compliance_dict}
    except Exception as e:
        # A broken rule engine must not become an open gate.
        logger.exception("Proposal %s: compliance engine error", proposal_id)
        proposal.status = "failed"
        proposal.error = f"compliance engine error: {e}"
        _record("blocked", rationale=f"compliance engine error: {e}")
        return {"ok": False,
                "error": f"Compliance could not be evaluated, so the order was not "
                         f"placed: {e}"}

    result = place_order_result(client, params)

    if result.get("ok"):
        proposal.status = "executed"
        proposal.order_id = result.get("order_id")
        logger.info("Proposal %s executed: order_id=%s", proposal_id, proposal.order_id)
        _record("executed", order_id=proposal.order_id, compliance=compliance_dict)
    else:
        proposal.status = "failed"
        proposal.error = result.get("error", "Unknown execution error")
        logger.warning("Proposal %s failed: %s", proposal_id, proposal.error)
        _record("failed", compliance=compliance_dict,
                rationale=f"broker rejected: {proposal.error}")

    return result


def _as_float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0
