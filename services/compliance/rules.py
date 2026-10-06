"""The rule engine: evaluate a proposed order against a dated ruleset.

Design notes worth knowing before changing this:

  * **No limit is hardcoded.** Everything numeric comes from the JSON ruleset,
    which carries an ``effective_from`` date and a ``source`` per rule. A rule
    change is then a data edit, and the ruleset in force on a past date can be
    reconstructed for an audit.
  * **Every breach cites its rule and source.** "Blocked by compliance" is not
    an answer anyone can act on; "blocked by single_issuer_cap, limit 10% of
    NAV, source sebi_master_circular" is.
  * **Missing context does not silently pass.** If a rule needs the portfolio
    NAV and none is supplied, the rule reports itself as not evaluated rather
    than quietly returning no breach. Those appear in ``not_evaluated`` so a
    caller can see what was *not* checked.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

logger = logging.getLogger(__name__)

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RULES_PATH = os.path.join(_repo_root, "data", "compliance_rules.json")

SEVERITY_BLOCK = "block"
SEVERITY_WARN = "warn"


@dataclass
class Breach:
    rule_id: str
    kind: str
    severity: str
    message: str
    limit: Optional[float] = None
    observed: Optional[float] = None
    source: str = ""
    citation: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        parts = [f"[{self.rule_id}] {self.message}"]
        if self.limit is not None and self.observed is not None:
            parts.append(f"(limit {self.limit}, observed {round(self.observed, 4)})")
        if self.citation:
            parts.append(f"— {self.citation}")
        return " ".join(parts)


@dataclass
class ComplianceResult:
    breaches: list[Breach] = field(default_factory=list)
    not_evaluated: list[dict] = field(default_factory=list)
    ruleset_id: str = ""
    effective_from: str = ""
    scheme: str = ""
    is_template: bool = False

    @property
    def blocked(self) -> bool:
        return any(b.severity == SEVERITY_BLOCK for b in self.breaches)

    @property
    def warnings(self) -> list[Breach]:
        return [b for b in self.breaches if b.severity == SEVERITY_WARN]

    def reason(self) -> str:
        blocking = [b for b in self.breaches if b.severity == SEVERITY_BLOCK]
        if not blocking:
            return ""
        return "; ".join(b.describe() for b in blocking)

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "ruleset_id": self.ruleset_id,
            "effective_from": self.effective_from,
            "scheme": self.scheme,
            "is_template": self.is_template,
            "breaches": [b.to_dict() for b in self.breaches],
            "not_evaluated": self.not_evaluated,
        }


# ── Ruleset loading ────────────────────────────────────────────────────────
_ruleset_cache: dict[str, Any] = {}


def load_ruleset(path: str = RULES_PATH, *, refresh: bool = False) -> dict:
    """Load and cache the ruleset. Warns loudly when it is still the template."""
    if not refresh and path in _ruleset_cache:
        return _ruleset_cache[path]

    if not os.path.exists(path):
        logger.warning("compliance: no ruleset at %s — nothing will be checked", path)
        return {}

    with open(path, encoding="utf-8") as f:
        ruleset = json.load(f)

    if str(ruleset.get("status", "")).upper().startswith("TEMPLATE"):
        logger.warning(
            "compliance: ruleset %r is a TEMPLATE. Its limits are placeholders and "
            "must be replaced with values cited to the current SEBI master circular "
            "and the scheme SID before governing a real trade.",
            ruleset.get("ruleset_id"),
        )

    _ruleset_cache[path] = ruleset
    return ruleset


def ruleset_info(path: str = RULES_PATH) -> dict:
    """Summary for the API and the decision record."""
    rs = load_ruleset(path)
    if not rs:
        return {"loaded": False}
    return {
        "loaded": True,
        "ruleset_id": rs.get("ruleset_id"),
        "effective_from": rs.get("effective_from"),
        "is_template": str(rs.get("status", "")).upper().startswith("TEMPLATE"),
        "schemes": sorted(rs.get("schemes", {})),
        "rules": sorted(rs.get("rules", {})),
    }


# ── Evaluation ─────────────────────────────────────────────────────────────
def check(
    order: dict,
    *,
    portfolio: Optional[dict] = None,
    scheme: str = "default",
    ruleset: Optional[dict] = None,
    path: str = RULES_PATH,
) -> ComplianceResult:
    """Evaluate one proposed order.

    ``order``     Angel-style params: tradingsymbol, transactiontype, quantity,
                  price, producttype, exchange.
    ``portfolio`` optional context: ``nav``, ``holdings`` (symbol -> value),
                  ``sector_exposure`` (sector -> value), ``adv`` (value traded).
    """
    rs = ruleset if ruleset is not None else load_ruleset(path)
    result = ComplianceResult(
        ruleset_id=rs.get("ruleset_id", ""),
        effective_from=rs.get("effective_from", ""),
        scheme=scheme,
        is_template=str(rs.get("status", "")).upper().startswith("TEMPLATE"),
    )
    if not rs:
        result.not_evaluated.append({"rule_id": "*", "reason": "no ruleset loaded"})
        return result

    scheme_cfg = (rs.get("schemes") or {}).get(scheme)
    if scheme_cfg is None:
        logger.warning("compliance: unknown scheme %r; falling back to 'default'", scheme)
        scheme_cfg = (rs.get("schemes") or {}).get("default", {"rules": []})
        result.scheme = "default"

    rules = rs.get("rules") or {}
    ctx = _context(order, portfolio or {})

    for rule_id in scheme_cfg.get("rules", []):
        rule = rules.get(rule_id)
        if rule is None:
            result.not_evaluated.append({"rule_id": rule_id, "reason": "not defined in ruleset"})
            continue

        handler = _HANDLERS.get(rule.get("kind"))
        if handler is None:
            result.not_evaluated.append({"rule_id": rule_id,
                                         "reason": f"no handler for kind {rule.get('kind')!r}"})
            continue

        try:
            breach, skipped = handler(rule_id, rule, ctx, scheme_cfg)
        except Exception as e:  # a buggy rule must not let a trade through
            logger.exception("compliance: rule %s raised", rule_id)
            result.not_evaluated.append({"rule_id": rule_id, "reason": f"rule error: {e}"})
            continue

        if skipped:
            result.not_evaluated.append({"rule_id": rule_id, "reason": skipped})
        if breach:
            result.breaches.append(breach)

    return result


def _context(order: dict, portfolio: dict) -> dict:
    symbol = _norm(order.get("tradingsymbol") or order.get("symbol") or "")
    qty = _num(order.get("quantity")) or 0.0
    price = _num(order.get("price")) or _num(order.get("triggerprice")) or 0.0
    side = str(order.get("transactiontype") or order.get("side") or "BUY").upper()

    return {
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "price": price,
        "order_value": qty * price,
        "product": str(order.get("producttype") or order.get("product") or "").upper(),
        "exchange": str(order.get("exchange") or "NSE").upper(),
        "nav": _num(portfolio.get("nav")),
        "holdings": portfolio.get("holdings") or {},
        "sector_exposure": portfolio.get("sector_exposure") or {},
        "sector": portfolio.get("sector") or _sector_of(symbol),
        "cap_band": portfolio.get("cap_band") or _cap_band_of(symbol),
        "adv": _num(portfolio.get("adv")),
    }


# ── Rule handlers ──────────────────────────────────────────────────────────
# Each returns (breach_or_None, skip_reason_or_None).

def _restricted_list(rule_id, rule, ctx, _scheme):
    symbols = {_norm(s) for s in rule.get("symbols", [])}
    if ctx["symbol"] in symbols:
        return Breach(
            rule_id=rule_id, kind=rule["kind"], severity=rule.get("severity", SEVERITY_BLOCK),
            message=rule.get("message", "{symbol} is restricted").format(**ctx),
            source=rule.get("source", ""), citation=rule.get("citation", ""),
        ), None
    return None, None


def _instrument(rule_id, rule, ctx, _scheme):
    allowed_products = {p.upper() for p in rule.get("allowed_products", [])}
    allowed_exchanges = {e.upper() for e in rule.get("allowed_exchanges", [])}

    if ctx["product"] and allowed_products and ctx["product"] not in allowed_products:
        return Breach(
            rule_id=rule_id, kind=rule["kind"], severity=rule.get("severity", SEVERITY_BLOCK),
            message=rule.get("message", "product not permitted").format(**ctx),
            source=rule.get("source", ""), citation=rule.get("citation", ""),
        ), None

    if ctx["exchange"] and allowed_exchanges and ctx["exchange"] not in allowed_exchanges:
        return Breach(
            rule_id=rule_id, kind=rule["kind"], severity=rule.get("severity", SEVERITY_BLOCK),
            message=f"Exchange {ctx['exchange']} is not permitted for this scheme.",
            source=rule.get("source", ""), citation=rule.get("citation", ""),
        ), None

    return None, None


def _cap_band(rule_id, rule, ctx, scheme_cfg):
    cfg = scheme_cfg.get("cap_band_eligibility") or {}
    allowed = {b.lower() for b in cfg.get("allowed_bands", [])}
    if not allowed:
        return None, "scheme defines no allowed cap bands"
    if not ctx["cap_band"]:
        return None, f"cap band unknown for {ctx['symbol']} (outside the indexed universe)"

    if ctx["side"] == "BUY" and ctx["cap_band"].lower() not in allowed:
        return Breach(
            rule_id=rule_id, kind=rule["kind"], severity=rule.get("severity", SEVERITY_BLOCK),
            message=rule.get("message", "cap band not permitted").format(
                allowed_bands=", ".join(sorted(allowed)), **ctx),
            source=cfg.get("source", rule.get("source", "")),
            citation=cfg.get("citation", rule.get("citation", "")),
        ), None
    return None, None


def _issuer_concentration(rule_id, rule, ctx, _scheme):
    if not ctx["nav"]:
        return None, "portfolio NAV not supplied; issuer concentration not checked"
    if ctx["side"] != "BUY":
        return None, None

    limit = _num(rule.get("limit_pct_of_nav"))
    if limit is None:
        return None, "rule defines no limit"

    existing = _num(ctx["holdings"].get(ctx["symbol"])) or 0.0
    projected_pct = (existing + ctx["order_value"]) / ctx["nav"] * 100.0

    if projected_pct > limit:
        return Breach(
            rule_id=rule_id, kind=rule["kind"], severity=rule.get("severity", SEVERITY_BLOCK),
            message=rule.get("message", "issuer limit exceeded").format(
                limit_pct_of_nav=limit, **ctx),
            limit=limit, observed=projected_pct,
            source=rule.get("source", ""), citation=rule.get("citation", ""),
        ), None
    return None, None


def _sector_concentration(rule_id, rule, ctx, scheme_cfg):
    if str(scheme_cfg.get("type", "")).lower() in {
        t.lower() for t in rule.get("exempt_scheme_types", [])
    }:
        return None, "scheme type is exempt from the sector cap"
    if not ctx["nav"]:
        return None, "portfolio NAV not supplied; sector concentration not checked"
    if not ctx["sector"]:
        return None, f"sector unknown for {ctx['symbol']}"
    if ctx["side"] != "BUY":
        return None, None

    limit = _num(rule.get("limit_pct_of_nav"))
    if limit is None:
        return None, "rule defines no limit"

    existing = _num(ctx["sector_exposure"].get(ctx["sector"])) or 0.0
    projected_pct = (existing + ctx["order_value"]) / ctx["nav"] * 100.0

    if projected_pct > limit:
        return Breach(
            rule_id=rule_id, kind=rule["kind"], severity=rule.get("severity", SEVERITY_BLOCK),
            message=rule.get("message", "sector limit exceeded").format(
                limit_pct_of_nav=limit, **ctx),
            limit=limit, observed=projected_pct,
            source=rule.get("source", ""), citation=rule.get("citation", ""),
        ), None
    return None, None


def _liquidity(rule_id, rule, ctx, _scheme):
    if not ctx["adv"]:
        return None, "average daily value traded not supplied; liquidity not checked"

    rate = _num(rule.get("participation_rate")) or 0.10
    max_days = _num(rule.get("max_days_to_liquidate"))
    if max_days is None:
        return None, "rule defines no limit"

    daily_capacity = ctx["adv"] * rate
    if daily_capacity <= 0:
        return None, "average daily value traded is zero"

    days = ctx["order_value"] / daily_capacity
    if days > max_days:
        return Breach(
            rule_id=rule_id, kind=rule["kind"], severity=rule.get("severity", SEVERITY_WARN),
            message=rule.get("message", "position is illiquid").format(
                days_to_liquidate=round(days, 2),
                participation_rate_pct=round(rate * 100, 1), **ctx),
            limit=max_days, observed=days,
            source=rule.get("source", ""), citation=rule.get("citation", ""),
        ), None
    return None, None


_HANDLERS = {
    "restricted_list": _restricted_list,
    "instrument": _instrument,
    "cap_band": _cap_band,
    "issuer_concentration": _issuer_concentration,
    "sector_concentration": _sector_concentration,
    "liquidity": _liquidity,
}


# ── Lookups ────────────────────────────────────────────────────────────────
def _cap_band_of(symbol: str) -> Optional[str]:
    try:
        from services.marketstore.universe import cap_band_of

        return cap_band_of(symbol)
    except Exception:
        return None


def _sector_of(symbol: str) -> Optional[str]:
    try:
        from services.marketstore.universe import industry_of

        return industry_of(symbol)
    except Exception:
        return None


def _norm(symbol: str) -> str:
    s = str(symbol).strip().upper()
    for suffix in ("-EQ", "-BE", ".NS"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def _num(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return None if f != f else f
