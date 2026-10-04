"""Tests for the deterministic fundamental score.

Guards the three things that make the score trustworthy: it reflects the quality
of the input, it degrades instead of crashing on missing data, and the bands in
``data/metric_glossary.json`` stay in sync with the keys the scorer reads.

Run directly (``python tests/test_fundamental_scoring.py``) or under pytest.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.fundamental_scoring import (
    GLOSSARY,
    METRICS,
    PILLAR_ORDER,
    SCORED_KEYS,
    VERDICT_POINTS,
    metric_verdicts,
    score_fundamentals,
)


def _strong_stock(**overrides) -> dict:
    """A high-quality, reasonably priced company."""
    base = {
        "symbol": "GOODCO-EQ",
        "sector": "Consumer Defensive",
        "industry": "Packaged Foods",
        "valuation": {
            "pe_ratio": 22.0,
            "forward_pe": 19.0,
            "pb_ratio": 2.4,
            "ev_ebitda": 9.0,
            "peg_ratio": 0.8,
            "dividend_yield": 1.8,
            "trailing_eps": 48.0,
        },
        "health": {
            "roe": 23.0,
            "roce": 26.0,
            "debt_to_equity": 18.0,
            "free_cash_flow": 4.2e9,
            "operating_cash_flow": 5.0e9,
            "total_debt": 1.0e9,
            "total_cash": 6.0e9,
            "revenue_growth": 18.0,
            "earnings_growth": 22.0,
            "profit_margin": 21.0,
            "operating_margin": 27.0,
            "promoter_holding": 58.0,
        },
        "revenue_trend": [
            {"year": "2021", "value": 1.0e10},
            {"year": "2022", "value": 1.2e10},
            {"year": "2023", "value": 1.4e10},
            {"year": "2024", "value": 1.7e10},
        ],
        "profit_trend": [
            {"year": "2021", "value": 1.5e9},
            {"year": "2022", "value": 2.0e9},
            {"year": "2023", "value": 2.6e9},
            {"year": "2024", "value": 3.5e9},
        ],
        "error": None,
    }
    base.update(overrides)
    return base


def _weak_stock(**overrides) -> dict:
    """Expensive, unprofitable, heavily indebted, shrinking."""
    base = {
        "symbol": "BADCO-EQ",
        "sector": "Industrials",
        "industry": "Engineering & Construction",
        "valuation": {
            "pe_ratio": 85.0,
            "pb_ratio": 9.0,
            "ev_ebitda": 40.0,
            "peg_ratio": 5.0,
        },
        "health": {
            "roe": 3.0,
            "roce": 4.0,
            "debt_to_equity": 310.0,
            "free_cash_flow": -2.0e9,
            "operating_cash_flow": -5.0e8,
            "total_debt": 8.0e9,
            "total_cash": 3.0e8,
            "revenue_growth": -8.0,
            "earnings_growth": -35.0,
            "profit_margin": 1.2,
            "operating_margin": 3.0,
            "promoter_holding": 22.0,
        },
        "revenue_trend": [
            {"year": "2021", "value": 9.0e9},
            {"year": "2022", "value": 8.0e9},
            {"year": "2023", "value": 7.5e9},
            {"year": "2024", "value": 6.9e9},
        ],
        "profit_trend": [
            {"year": "2024", "value": 8.0e7},
        ],
        "error": None,
    }
    base.update(overrides)
    return base


# ── Directional correctness ────────────────────────────────────────────────


def test_strong_stock_scores_well():
    score = score_fundamentals(_strong_stock())
    assert score["total"] >= 75, score
    assert score["grade"] in ("A", "B"), score["grade"]
    assert score["confidence"] == "high", score


def test_weak_stock_scores_badly():
    score = score_fundamentals(_weak_stock())
    assert score["total"] <= 40, score
    assert score["grade"] in ("D", "E"), score["grade"]


def test_strong_beats_weak_by_a_wide_margin():
    strong = score_fundamentals(_strong_stock())["total"]
    weak = score_fundamentals(_weak_stock())["total"]
    assert strong - weak >= 35, (strong, weak)


def test_score_is_deterministic():
    a = score_fundamentals(_strong_stock())
    b = score_fundamentals(_strong_stock())
    assert a == b


def test_total_always_within_0_100():
    for payload in (_strong_stock(), _weak_stock()):
        total = score_fundamentals(payload)["total"]
        assert 0 <= total <= 100, total


def test_pillars_are_bounded_by_their_max():
    for payload in (_strong_stock(), _weak_stock()):
        for pillar in score_fundamentals(payload)["pillars"]:
            if pillar["score"] is not None:
                assert 0 <= pillar["score"] <= pillar["max"], pillar


def test_strengths_and_concerns_are_populated_from_verdicts():
    strong = score_fundamentals(_strong_stock())
    weak = score_fundamentals(_weak_stock())
    assert strong["strengths"] and len(strong["strengths"]) <= 3
    assert weak["concerns"] and len(weak["concerns"]) <= 3


# ── Degraded input ─────────────────────────────────────────────────────────


def test_all_missing_metrics_does_not_crash():
    score = score_fundamentals({"symbol": "X", "valuation": {}, "health": {}})
    assert score["total"] is None
    assert score["confidence"] == "none"
    assert score["note"]


def test_error_payload_returns_empty_score():
    score = score_fundamentals({"error": "No data found for X.NS"})
    assert score["total"] is None
    assert score["metrics_used"] == 0


def test_non_dict_input_returns_empty_score():
    assert score_fundamentals(None)["total"] is None
    assert score_fundamentals([])["total"] is None


def test_sparse_metrics_flag_low_confidence():
    sparse = {
        "symbol": "SPARSE-EQ",
        "sector": "Industrials",
        "valuation": {"pe_ratio": 20.0},
        "health": {"roe": 16.0},
        "revenue_trend": [],
        "profit_trend": [],
    }
    score = score_fundamentals(sparse)
    assert score["total"] is not None
    assert score["confidence"] == "low", score
    assert score["metrics_used"] == 2, score


def test_nan_and_junk_values_are_treated_as_missing():
    payload = _strong_stock()
    payload["valuation"]["pe_ratio"] = float("nan")
    payload["valuation"]["pb_ratio"] = "N/A"
    payload["health"]["roe"] = None
    score = score_fundamentals(payload)
    rows = metric_verdicts(score)
    for key in ("pe_ratio", "pb_ratio", "roe"):
        assert rows[key]["verdict"] == "unknown", rows[key]
    assert score["total"] is not None


def test_missing_whole_pillar_still_scales_to_100():
    """A stock with no valuation data at all must not be capped at 75."""
    payload = _strong_stock()
    payload["valuation"] = {}
    score = score_fundamentals(payload)
    valuation = next(p for p in score["pillars"] if p["key"] == "valuation")
    assert valuation["score"] is None
    assert score["total"] >= 75, score


def test_zero_profit_base_skips_cash_conversion():
    """Cash conversion against a loss is meaningless, not 'bad'."""
    payload = _strong_stock()
    payload["profit_trend"] = [{"year": "2024", "value": -1.0e9}]
    payload["health"]["profit_margin"] = -5.0
    rows = metric_verdicts(score_fundamentals(payload))
    assert rows["cash_conversion"]["verdict"] == "unknown"


def test_revenue_consistency_needs_three_years():
    payload = _strong_stock()
    payload["revenue_trend"] = [{"year": "2024", "value": 1.0e10}]
    rows = metric_verdicts(score_fundamentals(payload))
    assert rows["revenue_consistency"]["verdict"] == "unknown"


# ── Sector overrides ───────────────────────────────────────────────────────


def test_bank_excludes_leverage_metrics_and_still_totals_out_of_100():
    bank = _strong_stock(sector="Financial Services", industry="Banks - Regional")
    bank["health"]["debt_to_equity"] = 820.0  # routine for a lender
    score = score_fundamentals(bank)

    assert score["sector_profile"] == "financial"
    for key in ("debt_to_equity", "ev_ebitda", "roce", "free_cash_flow"):
        assert key in score["excluded"], key

    rows = metric_verdicts(score)
    assert rows["debt_to_equity"]["verdict"] == "excluded"
    # The 8.2x leverage must not drag the score down.
    assert score["total"] >= 70, score
    assert 0 <= score["total"] <= 100
    # Excluded metrics are not counted against coverage.
    assert score["metrics_total"] < len(SCORED_KEYS)


def test_bank_balance_sheet_pillar_is_skipped_not_scored_on_cash_flow():
    """A lender growing its loan book shows negative free cash flow exactly when
    it is doing well. Scoring that pillar on FCF alone would be a false signal,
    so the whole pillar reports no data and the total rescales."""
    bank = _strong_stock(sector="Financial Services", industry="Banks - Regional")
    bank["health"]["free_cash_flow"] = -2.4e11  # normal for a growing lender

    score = score_fundamentals(bank)
    balance = next(p for p in score["pillars"] if p["key"] == "balance_sheet")

    assert balance["score"] is None, balance
    assert all(m["verdict"] == "excluded" for m in balance["metrics"]), balance
    # The negative cash flow must not appear as a concern or drag the total.
    assert not any("Free Cash Flow" in c for c in score["concerns"]), score["concerns"]
    assert score["total"] >= 80, score


def test_bank_is_not_penalised_for_low_roce():
    """ROCE's denominator for a lender is mostly customer deposits, so a healthy
    bank reads as sub-cost-of-capital. It must not surface as a concern."""
    bank = _strong_stock(sector="Financial Services", industry="Banks - Regional")
    bank["health"]["roce"] = 7.9  # typical for a well-run Indian bank

    score = score_fundamentals(bank)
    assert metric_verdicts(score)["roce"]["verdict"] == "excluded"
    assert not any("ROCE" in c for c in score["concerns"]), score["concerns"]

    # A non-financial with the same ROCE still gets flagged.
    industrial = _strong_stock(sector="Industrials", industry="Engineering & Construction")
    industrial["health"]["roce"] = 7.9
    assert metric_verdicts(score_fundamentals(industrial))["roce"]["verdict"] == "bad"


def test_it_sector_tolerates_high_price_to_book():
    generic = _strong_stock(sector="Industrials", industry="Specialty Industrial Machinery")
    generic["valuation"]["pb_ratio"] = 12.0
    it = _strong_stock(sector="Technology", industry="Information Technology Services")
    it["valuation"]["pb_ratio"] = 12.0

    assert metric_verdicts(score_fundamentals(generic))["pb_ratio"]["verdict"] == "bad"
    assert metric_verdicts(score_fundamentals(it))["pb_ratio"]["verdict"] == "good"


def test_unknown_sector_uses_generic_bands():
    score = score_fundamentals(_strong_stock(sector="", industry=""))
    assert score["sector_profile"] is None
    assert score["excluded"] == []


# ── Glossary integrity ─────────────────────────────────────────────────────


def test_every_scored_metric_has_bands_and_prose():
    for key in SCORED_KEYS:
        meta = METRICS[key]
        assert meta.get("bands"), f"{key} is scored but has no bands"
        for field in ("label", "full_name", "pillar", "plain", "ideal"):
            assert meta.get(field), f"{key} is missing '{field}'"


def test_every_metric_belongs_to_a_known_pillar():
    pillars = set(GLOSSARY.get("pillars", {}))
    assert set(PILLAR_ORDER) <= pillars
    for key, meta in METRICS.items():
        assert meta["pillar"] in pillars, f"{key} -> unknown pillar {meta['pillar']}"


def test_display_only_metrics_are_never_scored():
    """Weight-0 metrics still need teaching copy, but must stay out of the score."""
    for key, meta in METRICS.items():
        if (meta.get("weight") or 0) == 0:
            assert key not in SCORED_KEYS
            assert meta.get("plain"), f"{key} has no explanation for the Learn page"


def test_bands_are_ordered_and_end_with_a_catch_all():
    for key in SCORED_KEYS:
        bands = METRICS[key]["bands"]
        assert bands[-1].get("max") is None, f"{key} has no catch-all band"
        ceilings = [b["max"] for b in bands[:-1]]
        assert all(c is not None for c in ceilings), f"{key} has a null max mid-list"
        assert ceilings == sorted(ceilings), f"{key} bands are out of order: {ceilings}"


def test_every_band_resolves_to_a_known_verdict():
    for key in SCORED_KEYS:
        for band in METRICS[key]["bands"]:
            assert band["verdict"] in VERDICT_POINTS, (key, band)
            assert band.get("note"), f"{key} band {band['max']} has no note"


def test_explicit_band_points_are_sane():
    """A band may override its award, but only within 0-1 and without
    out-ranking a better verdict."""
    for key in SCORED_KEYS:
        for band in METRICS[key]["bands"]:
            if "points" not in band:
                continue
            pts = band["points"]
            assert isinstance(pts, (int, float)), (key, band)
            assert 0.0 <= pts <= 1.0, (key, band)
            if band["verdict"] == "bad":
                assert pts <= VERDICT_POINTS["neutral"], (key, band)
            elif band["verdict"] == "neutral":
                assert pts <= VERDICT_POINTS["good"], (key, band)


def test_an_excellent_company_can_still_reach_the_top_grade():
    """Tuned 'good' bands must not make a perfect score unreachable."""
    score = score_fundamentals(_strong_stock())
    assert score["grade"] == "A", score
    # ...but a merely solid company should not be indistinguishable from it.
    solid = _strong_stock()
    solid["health"].update(roe=17.0, roce=16.0, profit_margin=12.0, operating_margin=18.0)
    solid["valuation"].update(ev_ebitda=15.0)
    assert score_fundamentals(solid)["total"] < score["total"]


def test_boundary_values_resolve_without_a_gap():
    """A value sitting exactly on a band edge must grade, and grade stably."""
    from services.fundamental_scoring import _bands_for, _grade_value

    for key in SCORED_KEYS:
        bands = _bands_for(key, None)
        for band in bands:
            if band["max"] is None:
                continue
            for probe in (band["max"], band["max"] - 1e-9):
                verdict, note, points = _grade_value(probe, bands)
                assert verdict in VERDICT_POINTS, (key, probe, verdict)
                assert note, (key, probe)
                assert 0.0 <= points <= 1.0, (key, probe, points)
        # Extremes on both ends always land somewhere.
        for probe in (-1e12, 1e12):
            assert _grade_value(probe, bands)[0] in VERDICT_POINTS, (key, probe)


def test_grade_bands_cover_zero_to_one_hundred():
    grades = GLOSSARY["scoring"]["grades"]
    mins = [g["min"] for g in grades]
    assert mins == sorted(mins, reverse=True), "grades must descend"
    assert mins[-1] == 0, "lowest grade must start at 0"
    for g in grades:
        assert g.get("grade") and g.get("label")


def test_learn_page_content_is_present():
    """The /learn page renders straight from this file; guard its sections."""
    assert len(GLOSSARY.get("intro", [])) >= 4
    for item in GLOSSARY["intro"]:
        assert item.get("title") and item.get("body")
    scoring = GLOSSARY["scoring"]
    assert len(scoring.get("how_it_works", [])) >= 3
    assert scoring.get("limits")
    for key in PILLAR_ORDER:
        pillar = GLOSSARY["pillars"][key]
        assert pillar.get("label") and pillar.get("tagline") and pillar.get("body")


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
