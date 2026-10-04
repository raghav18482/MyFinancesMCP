"""Tests for the payload sent to the LLM for a stock summary.

The prompt is the only thing standing between a correct score and a summary
that contradicts it, so these guard the two ways that can go wrong: leaking
metrics the sector override deliberately suppressed, and leaking Python ``None``
into text the model reads as a value.

Run directly (``python tests/test_fundamental_prompt.py``) or under pytest.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.ai_service import (
    FUNDAMENTAL_SYSTEM_PROMPT,
    build_fundamental_prompt_payload,
)
from services.fundamental_scoring import score_fundamentals
from tests.test_fundamental_scoring import _strong_stock, _weak_stock


def _scored(payload: dict) -> dict:
    payload = dict(payload)
    payload["score"] = score_fundamentals(payload)
    return payload


def _bank() -> dict:
    bank = _strong_stock(sector="Financial Services", industry="Banks - Regional")
    bank["health"].update(debt_to_equity=812.0, roce=7.9, free_cash_flow=-2.4e11)
    return _scored(bank)


# ── Sector exclusions must not leak ────────────────────────────────────────


def test_excluded_metrics_are_quarantined_not_listed_as_health():
    payload = build_fundamental_prompt_payload(_bank())

    for key in ("debt_to_equity", "roce", "free_cash_flow"):
        assert key not in payload["financial_health"], key
        assert key not in payload["metric_glossary"], key

    quarantine = payload["DO_NOT_JUDGE_THESE"]
    assert "Debt to Equity" in quarantine["values"]
    assert quarantine["values"]["Debt to Equity"] == 812.0
    assert "not meaningful" in quarantine["why"]


def test_excluded_metrics_never_appear_as_graded():
    payload = build_fundamental_prompt_payload(_bank())
    graded = {m["metric"] for m in payload["graded_metrics"]}
    for label in ("Debt to Equity", "ROCE", "Free Cash Flow"):
        assert label not in graded, label


def test_the_alarming_number_is_not_adjacent_to_real_metrics():
    """A bank's 812 D/E must not sit in the same block as its real figures —
    that is what invites 'its debt is alarming' in the summary."""
    payload = build_fundamental_prompt_payload(_bank())
    rendered = json.dumps({k: v for k, v in payload.items() if k != "DO_NOT_JUDGE_THESE"})
    assert "812" not in rendered, "excluded D/E leaked outside the quarantine block"


def test_non_financial_has_no_quarantine_block():
    payload = build_fundamental_prompt_payload(_scored(_strong_stock()))
    assert "DO_NOT_JUDGE_THESE" not in payload
    assert payload["financial_health"]["debt_to_equity"] == 18.0


# ── No raw None in anything the model reads as a value ─────────────────────


def test_unscored_pillar_reads_as_prose_not_none():
    payload = build_fundamental_prompt_payload(_bank())
    balance = payload["score"]["pillars"]["Balance sheet"]
    assert "None" not in balance, balance
    assert "not scored" in balance

    # The scored pillars still read as a fraction.
    assert payload["score"]["pillars"]["Valuation"].endswith("/ 25")


def test_no_literal_none_anywhere_in_the_prompt():
    for payload in (_bank(), _scored(_strong_stock()), _scored(_weak_stock())):
        rendered = json.dumps(build_fundamental_prompt_payload(payload), default=str)
        assert '"None' not in rendered, rendered[:400]
        assert "None /" not in rendered, rendered[:400]


def test_missing_metrics_are_named_so_the_model_can_say_so():
    sparse = _scored({
        "symbol": "SPARSE-EQ", "company_name": "Thin Data Ltd", "sector": "Basic Materials",
        "valuation": {"pe_ratio": 21.0}, "health": {"roe": 14.2},
        "revenue_trend": [], "profit_trend": [],
    })
    payload = build_fundamental_prompt_payload(sparse)
    assert "Free Cash Flow" in payload["metrics_with_no_data"]
    assert payload["score"]["confidence"] == "low"


# ── Values and glossary ────────────────────────────────────────────────────


def test_graded_metrics_carry_display_value_and_assessment():
    payload = build_fundamental_prompt_payload(_scored(_strong_stock()))
    roe = next(m for m in payload["graded_metrics"] if m["metric"] == "ROE")
    assert roe["value"] == "23.00%"
    assert roe["verdict"] == "good"
    assert roe["assessment"]


def test_glossary_supplies_meaning_and_ideal_for_present_metrics():
    payload = build_fundamental_prompt_payload(_scored(_strong_stock()))
    pe = payload["metric_glossary"]["pe_ratio"]
    assert pe["means"] and pe["ideal"]
    # Only metrics that actually have a value get an entry.
    assert set(payload["metric_glossary"]) <= set(payload["valuation"]) | set(payload["financial_health"])


def test_chart_arrays_become_year_keyed_maps():
    payload = build_fundamental_prompt_payload(_scored(_strong_stock()))
    assert payload["revenue_by_year"]["2024"] == 1.7e10
    assert payload["net_profit_by_year"]["2024"] == 3.5e9


def test_payload_is_json_serialisable():
    for payload in (_bank(), _scored(_weak_stock())):
        json.dumps(build_fundamental_prompt_payload(payload))


def test_system_prompt_states_the_beginner_rules():
    for rule in ("beginner", "ONLY the numbers", "DO_NOT_JUDGE_THESE",
                 "not scored", "This is not financial advice."):
        assert rule in FUNDAMENTAL_SYSTEM_PROMPT, rule


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
