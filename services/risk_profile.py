"""
Client risk profile storage and validation.

In-memory, keyed by web session id. Same lifecycle as SessionManager.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

_VALID_GOALS = {"wealth creation", "income", "preservation", "speculation"}
_VALID_TOLERANCES = {"low", "medium", "high"}
_VALID_PRODUCTS = {"DELIVERY", "INTRADAY"}

_TOLERANCE_DEFAULTS: dict[str, dict[str, Any]] = {
    "low": {
        "max_single_order_value": 25_000,
        "max_position_pct": 10.0,
        "allowed_products": ["DELIVERY"],
        "max_daily_trades": 3,
    },
    "medium": {
        "max_single_order_value": 100_000,
        "max_position_pct": 20.0,
        "allowed_products": ["DELIVERY", "INTRADAY"],
        "max_daily_trades": 10,
    },
    "high": {
        "max_single_order_value": 500_000,
        "max_position_pct": 35.0,
        "allowed_products": ["DELIVERY", "INTRADAY"],
        "max_daily_trades": 25,
    },
}


@dataclass
class ClientRiskProfile:
    age: int
    goal: str
    horizon_years: int
    risk_tolerance: str
    tax_bracket: str
    max_single_order_value: float
    max_position_pct: float
    allowed_products: list[str] = field(default_factory=lambda: ["DELIVERY"])
    max_daily_trades: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "age": self.age,
            "goal": self.goal,
            "horizon_years": self.horizon_years,
            "risk_tolerance": self.risk_tolerance,
            "tax_bracket": self.tax_bracket,
            "max_single_order_value": self.max_single_order_value,
            "max_position_pct": self.max_position_pct,
            "allowed_products": self.allowed_products,
            "max_daily_trades": self.max_daily_trades,
        }


def build_profile_from_dict(data: dict[str, Any]) -> ClientRiskProfile:
    """Validate and create a ClientRiskProfile from raw form/JSON input."""
    age = int(data.get("age", 0))
    if age < 18 or age > 120:
        raise ValueError("age must be between 18 and 120")

    goal = str(data.get("goal", "")).strip().lower()
    if goal not in _VALID_GOALS:
        raise ValueError(f"goal must be one of: {', '.join(sorted(_VALID_GOALS))}")

    horizon = int(data.get("horizon_years", 0))
    if horizon < 1 or horizon > 50:
        raise ValueError("horizon_years must be between 1 and 50")

    tolerance = str(data.get("risk_tolerance", "")).strip().lower()
    if tolerance not in _VALID_TOLERANCES:
        raise ValueError(f"risk_tolerance must be one of: {', '.join(sorted(_VALID_TOLERANCES))}")

    tax_bracket = str(data.get("tax_bracket", "0%")).strip()

    defaults = _TOLERANCE_DEFAULTS[tolerance]

    max_order = float(data.get("max_single_order_value", defaults["max_single_order_value"]))
    max_pos = float(data.get("max_position_pct", defaults["max_position_pct"]))
    products_raw = data.get("allowed_products", defaults["allowed_products"])
    if isinstance(products_raw, str):
        products_raw = [p.strip() for p in products_raw.split(",")]
    products = [p.upper() for p in products_raw if p.upper() in _VALID_PRODUCTS]
    if not products:
        products = defaults["allowed_products"]
    max_trades = int(data.get("max_daily_trades", defaults["max_daily_trades"]))

    return ClientRiskProfile(
        age=age,
        goal=goal,
        horizon_years=horizon,
        risk_tolerance=tolerance,
        tax_bracket=tax_bracket,
        max_single_order_value=max_order,
        max_position_pct=max_pos,
        allowed_products=products,
        max_daily_trades=max_trades,
    )


class RiskProfileStore:
    """In-memory risk profile store, keyed by web session id."""

    def __init__(self) -> None:
        self._profiles: dict[str, ClientRiskProfile] = {}
        self._lock = threading.Lock()

    def set(self, session_id: str, profile: ClientRiskProfile) -> None:
        with self._lock:
            self._profiles[session_id] = profile
        logger.info("Risk profile set for session %s", session_id[:8])

    def get(self, session_id: str) -> Optional[ClientRiskProfile]:
        with self._lock:
            return self._profiles.get(session_id)

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._profiles.pop(session_id, None)

    def has(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._profiles


risk_profiles = RiskProfileStore()
