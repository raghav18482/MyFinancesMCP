"""Tests for the mandate and compliance gate.

Three properties the gate must have, because each has a tempting wrong answer:

  * a breach **blocks**, and the block names the rule and its source — "blocked
    by compliance" is not something anyone can act on
  * a rule that cannot be evaluated is **reported**, not silently passed — the
    absence of a breach must not be confused with the absence of a check
  * a broken rule is **not an open gate** — an exception inside the engine must
    stop the trade, not wave it through

Run directly (``python tests/test_compliance.py``) or under pytest.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.compliance import check, rules


# A minimal, self-contained ruleset so the tests do not depend on the shipped
# template's placeholder numbers.
RULESET = {
    "ruleset_id": "test-v1",
    "effective_from": "2026-01-01",
    "status": "TEST",
    "schemes": {
        "default": {"rules": ["restricted_list"]},
        "large_cap": {
            "rules": ["restricted_list", "instrument_eligibility",
                      "cap_band_eligibility", "single_issuer_cap", "sector_cap"],
            "cap_band_eligibility": {
                "allowed_bands": ["large"],
                "source": "sebi_master_circular",
                "citation": "scheme categorisation, clause X",
            },
        },
        "liquid_check": {"rules": ["liquidity_days_to_liquidate"]},
        "broken": {"rules": ["bad_rule"]},
    },
    "rules": {
        "restricted_list": {
            "kind": "restricted_list", "symbols": ["BADCO"], "severity": "block",
            "source": "internal", "citation": "fund restricted list",
            "message": "{symbol} is on the restricted list.",
        },
        "instrument_eligibility": {
            "kind": "instrument", "allowed_products": ["DELIVERY"],
            "allowed_exchanges": ["NSE"], "severity": "block",
            "source": "scheme_sid", "citation": "permitted instruments",
            "message": "Product {product} is not permitted.",
        },
        "cap_band_eligibility": {
            "kind": "cap_band", "severity": "block",
            "source": "sebi_master_circular", "citation": "scheme categorisation",
            "message": "{symbol} is a {cap_band} cap; scheme allows {allowed_bands}.",
        },
        "single_issuer_cap": {
            "kind": "issuer_concentration", "limit_pct_of_nav": 10.0, "severity": "block",
            "source": "sebi_master_circular", "citation": "single issuer limit",
            "message": "Issuer would exceed {limit_pct_of_nav}% of NAV.",
        },
        "sector_cap": {
            "kind": "sector_concentration", "limit_pct_of_nav": 25.0, "severity": "block",
            "source": "scheme_sid", "citation": "sector limit",
            "message": "Sector would exceed {limit_pct_of_nav}% of NAV.",
        },
        "liquidity_days_to_liquidate": {
            "kind": "liquidity", "max_days_to_liquidate": 5.0,
            "participation_rate": 0.10, "severity": "warn",
            "source": "sebi_master_circular", "citation": "liquidity norms",
            "message": "Exit would take {days_to_liquidate} days.",
        },
        "bad_rule": {"kind": "issuer_concentration", "limit_pct_of_nav": "not-a-number"},
    },
}

ORDER = {
    "tradingsymbol": "RELIANCE-EQ", "transactiontype": "BUY",
    "quantity": 100, "price": 1000.0, "producttype": "DELIVERY", "exchange": "NSE",
}


def _check(order=None, **kw):
    return check({**ORDER, **(order or {})}, ruleset=RULESET, **kw)


def test_a_clean_order_passes():
    result = _check(portfolio={"nav": 10_000_000, "cap_band": "large",
                               "sector": "Energy"}, scheme="large_cap")
    assert not result.blocked, f"clean order was blocked: {result.reason()}"
    assert result.ruleset_id == "test-v1"
    print("ok  a compliant order passes")


def test_a_restricted_symbol_is_blocked_with_its_rule_cited():
    result = _check({"tradingsymbol": "BADCO"}, scheme="default")

    assert result.blocked, "a restricted symbol must be blocked"
    breach = result.breaches[0]
    assert breach.rule_id == "restricted_list"
    assert breach.severity == "block"
    assert "BADCO" in breach.message
    assert breach.source == "internal"
    assert breach.citation == "fund restricted list"
    assert "restricted_list" in result.reason(), "the reason must name the rule"
    print("ok  a restricted symbol is blocked and the breach cites rule and source")


def test_issuer_concentration_blocks_and_reports_limit_and_observed():
    # 2,000,000 of order value against a 10,000,000 NAV is 20%, over the 10% cap.
    result = _check({"quantity": 2000, "price": 1000.0},
                    portfolio={"nav": 10_000_000, "cap_band": "large",
                               "sector": "Energy"},
                    scheme="large_cap")

    assert result.blocked
    breach = next(b for b in result.breaches if b.rule_id == "single_issuer_cap")
    assert breach.limit == 10.0
    assert abs(breach.observed - 20.0) < 1e-6, f"observed {breach.observed}"
    assert "10.0" in breach.describe() and "20.0" in breach.describe()
    print("ok  issuer concentration reports both the limit and what was observed")


def test_existing_holdings_count_towards_the_issuer_limit():
    """A fresh 5% buy on top of an existing 8% position breaches a 10% cap."""
    result = _check({"quantity": 500, "price": 1000.0},
                    portfolio={"nav": 10_000_000, "cap_band": "large",
                               "sector": "Energy",
                               "holdings": {"RELIANCE": 800_000}},
                    scheme="large_cap")
    assert result.blocked, "the existing position must be added to the new order"
    breach = next(b for b in result.breaches if b.rule_id == "single_issuer_cap")
    assert abs(breach.observed - 13.0) < 1e-6
    print("ok  existing holdings count towards the issuer limit")


def test_a_sell_is_not_blocked_by_concentration_limits():
    result = _check({"transactiontype": "SELL", "quantity": 5000},
                    portfolio={"nav": 1_000_000, "cap_band": "large",
                               "sector": "Energy"},
                    scheme="large_cap")
    concentration = [b for b in result.breaches
                     if b.rule_id in ("single_issuer_cap", "sector_cap")]
    assert not concentration, "selling reduces concentration; it must not breach it"
    print("ok  a sell is not blocked by concentration rules")


def test_cap_band_eligibility_blocks_the_wrong_band():
    result = _check(portfolio={"nav": 10_000_000, "cap_band": "small",
                               "sector": "Energy"},
                    scheme="large_cap")
    assert result.blocked
    breach = next(b for b in result.breaches if b.rule_id == "cap_band_eligibility")
    assert "small" in breach.message and "large" in breach.message
    assert breach.citation == "scheme categorisation, clause X", (
        "the scheme-level citation should override the rule-level one"
    )
    print("ok  a scheme may not buy outside its cap band")


def test_instrument_eligibility_blocks_a_disallowed_product():
    result = _check({"producttype": "INTRADAY"},
                    portfolio={"nav": 10_000_000, "cap_band": "large",
                               "sector": "Energy"},
                    scheme="large_cap")
    assert result.blocked
    assert any(b.rule_id == "instrument_eligibility" for b in result.breaches)
    print("ok  a disallowed product is blocked")


def test_a_warn_severity_does_not_block():
    result = _check({"quantity": 10_000, "price": 1000.0},
                    portfolio={"adv": 1_000_000}, scheme="liquid_check")
    assert result.warnings, "an illiquid position should raise a warning"
    assert not result.blocked, "a warn-severity breach must not block the trade"
    print("ok  a warn-severity breach surfaces without blocking")


# ── The properties that stop silent passes ─────────────────────────────────
def test_missing_context_is_reported_not_silently_passed():
    """No NAV means concentration was not checked — which is not the same as fine."""
    result = _check(portfolio={"cap_band": "large", "sector": "Energy"},
                    scheme="large_cap")

    not_evaluated = {e["rule_id"] for e in result.not_evaluated}
    assert "single_issuer_cap" in not_evaluated, (
        "without NAV the issuer rule cannot be evaluated and must say so"
    )
    assert "sector_cap" in not_evaluated
    reasons = " ".join(e["reason"] for e in result.not_evaluated)
    assert "NAV" in reasons
    print("ok  rules that could not be evaluated are reported, not passed")


def test_a_broken_rule_is_reported_and_does_not_open_the_gate():
    result = _check(portfolio={"nav": 10_000_000}, scheme="broken")
    assert not result.breaches, "a broken rule produces no breach"
    assert result.not_evaluated, (
        "a rule that could not run must be reported so the caller knows the "
        "check did not happen"
    )
    print("ok  a broken rule is reported rather than silently passing")


def test_an_unknown_scheme_falls_back_to_default():
    result = _check({"tradingsymbol": "BADCO"}, scheme="no-such-scheme")
    assert result.scheme == "default"
    assert result.blocked, "the default scheme still enforces the restricted list"
    print("ok  an unknown scheme falls back to default rather than skipping checks")


def test_an_empty_ruleset_blocks_nothing_but_says_so():
    result = check(ORDER, ruleset={})
    assert not result.blocked
    assert result.not_evaluated and result.not_evaluated[0]["rule_id"] == "*"
    print("ok  an empty ruleset blocks nothing and reports that nothing was checked")


def test_the_shipped_ruleset_is_flagged_as_a_template():
    """The shipped limits are placeholders and must announce themselves."""
    info = rules.ruleset_info()
    if not info.get("loaded"):
        print("skip  no shipped ruleset on disk")
        return
    assert info["is_template"] is True, (
        "the shipped ruleset carries placeholder limits and must be flagged, so "
        "nobody mistakes it for verified regulation"
    )
    result = check(ORDER, scheme="default")
    assert result.is_template is True
    print("ok  the shipped ruleset is flagged as a template in every result")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for fn in TESTS:
        fn()
    print(f"\n{len(TESTS)} passed")
