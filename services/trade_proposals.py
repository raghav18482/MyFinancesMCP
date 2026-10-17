"""
Historical trade-proposal store (read-only).

This application no longer executes trades. Earlier versions let the trading
agent create proposals that a person could approve into a live order; that
path has been removed permanently, along with proposal approval and the
server-side execution function. What remains is the in-memory history (list, reject,
expiry cleanup) and :func:`validate_against_profile`, a pure check used to
describe how a *hypothetical* idea compares with the user's risk profile.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from services.risk_profile import ClientRiskProfile

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

    def get(self, proposal_id: str) -> Optional[TradeProposal]:
        with self._lock:
            return self._proposals.get(proposal_id)

    def list_for_session(self, session_id: str) -> list[TradeProposal]:
        with self._lock:
            return [
                p for p in self._proposals.values()
                if p.session_id == session_id
            ]


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


def _as_float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0
