"""Deterministic 0-100 fundamental score.

Grades the dict produced by :func:`services.fundamental_service.get_stock_fundamentals`
against the bands in ``data/metric_glossary.json`` — the same file the ``/learn``
page renders, so the threshold used to grade a metric and the "ideal range" shown
to the user can never drift apart.

No network, no LLM: pure arithmetic over the metrics already fetched. A missing
metric is excluded rather than penalised, and the resulting coverage is reported
as ``confidence`` so the UI can label a thinly-sourced score as provisional.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GLOSSARY_PATH = os.path.join(_repo_root, "data", "metric_glossary.json")

try:
    with open(_GLOSSARY_PATH, encoding="utf-8") as f:
        GLOSSARY: dict[str, Any] = json.load(f)
except Exception as e:  # pragma: no cover - degraded mode, see _empty_score
    logger.warning("metric_glossary.json unavailable (%s) — scoring disabled", e)
    GLOSSARY = {}

METRICS: dict[str, dict] = GLOSSARY.get("metrics", {})
PILLARS: dict[str, dict] = GLOSSARY.get("pillars", {})
GRADES: list[dict] = GLOSSARY.get("scoring", {}).get("grades", [])

# Pillar order as presented in the UI.
PILLAR_ORDER = ["valuation", "profitability", "balance_sheet", "growth"]

# verdict -> share of the metric's weight awarded
VERDICT_POINTS = {"good": 1.0, "neutral": 0.55, "bad": 0.10}

# Metrics that carry weight 0 in the glossary are display-only (dividend yield,
# EPS, market cap, forward P/E): shown on the page and in the LLM prompt, never
# scored. This keeps the scored set explicit.
SCORED_KEYS = [k for k, m in METRICS.items() if (m.get("weight") or 0) > 0]

# Sector-specific corrections. ``drop`` removes a metric from scoring entirely;
# ``bands`` replaces its bands for that sector only.
SECTOR_OVERRIDES: dict[str, dict] = {
    # Borrowing is a lender's raw material, not a risk signal, and EV/EBITDA is
    # not meaningful when interest is revenue rather than a financing cost.
    #
    # This drops the whole balance-sheet pillar, which is deliberate: a bank's
    # solvency lives in capital adequacy, gross/net NPAs and provision coverage,
    # none of which yfinance reports. Scoring it on free cash flow instead is
    # worse than not scoring it — a lender growing its loan book shows deeply
    # negative cash flow precisely when it is doing well. The pillar reports
    # "no data" and the total rescales across the three that remain.
    "financial": {
        "match": (
            "bank", "financial service", "financial conglomerate", "nbfc",
            "credit service", "capital market", "insurance", "asset management",
            "mortgage",
        ),
        # ROCE goes too: its denominator is capital employed, which for a lender
        # is mostly customer deposits. That inflates the base and makes every
        # healthy bank look like it earns below its cost of capital. Banks are
        # judged on ROE and net interest margin instead, both still scored.
        "drop": (
            "ev_ebitda", "debt_to_equity", "net_cash", "cash_conversion",
            "free_cash_flow", "roce",
        ),
    },
    # Asset-light businesses carry little book value by construction, so the
    # generic price-to-book ceiling would mark every healthy one down.
    "asset_light": {
        "match": (
            "technology", "software", "information technology", "communication services",
            "it services", "consulting",
        ),
        "bands": {
            "pb_ratio": [
                {"max": 0, "verdict": "bad", "note": "Negative book value"},
                {"max": 8, "verdict": "good", "note": "Reasonable for an asset-light business"},
                {"max": 15, "verdict": "good", "note": "Normal for this sector"},
                {"max": 25, "verdict": "neutral", "note": "Rich even for an asset-light business"},
                {"max": None, "verdict": "bad", "note": "Very high relative to net assets"},
            ],
        },
    },
}


def _sector_profile(sector: str, industry: str = "") -> Optional[str]:
    """Which override profile, if any, applies to this stock."""
    haystack = f"{sector or ''} {industry or ''}".lower()
    if not haystack.strip():
        return None
    for name, rules in SECTOR_OVERRIDES.items():
        if any(token in haystack for token in rules["match"]):
            return name
    return None


def _bands_for(key: str, profile: Optional[str]) -> list[dict]:
    """Bands for a metric, honouring any sector override."""
    if profile:
        override = SECTOR_OVERRIDES[profile].get("bands", {}).get(key)
        if override:
            return override
    return METRICS.get(key, {}).get("bands") or []


def _dropped_for(profile: Optional[str]) -> tuple[str, ...]:
    if not profile:
        return ()
    return SECTOR_OVERRIDES[profile].get("drop", ())


def _grade_value(value: float, bands: list[dict]) -> tuple[str, str, float]:
    """Grade a value against its bands.

    First band whose ``max`` covers the value wins; ``max: null`` is the
    catch-all. Returns ``(verdict, note, points)`` where points is the share of
    the metric's weight to award. A band may set ``points`` explicitly to
    separate "fair" from "excellent" without needing a fourth verdict colour —
    otherwise the verdict's default from :data:`VERDICT_POINTS` applies.
    """
    for band in bands:
        ceiling = band.get("max")
        if ceiling is None or value <= ceiling:
            break
    else:
        # No catch-all band: fall back to the last one rather than guessing.
        if not bands:
            return "neutral", "", VERDICT_POINTS["neutral"]
        band = bands[-1]

    verdict = band.get("verdict", "neutral")
    points = band.get("points")
    if points is None:
        points = VERDICT_POINTS.get(verdict, VERDICT_POINTS["neutral"])
    return verdict, band.get("note", ""), float(points)


def _num(val) -> Optional[float]:
    """Coerce to float, rejecting None/NaN/non-numeric."""
    if val is None or isinstance(val, bool):
        return None
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return out


def _fmt(value: float, unit: str) -> str:
    """Human-readable value, matching how research.js renders the same metric."""
    if unit == "%":
        return f"{value:.2f}%"
    if unit == "x":
        return f"{value:.2f}x"
    if unit == "x100":
        return f"{value:.2f} ({value / 100:.2f}x)"
    if unit == "₹":
        sign = "-" if value < 0 else ""
        mag = abs(value)
        if mag >= 1e7:
            return f"{sign}₹{mag / 1e7:,.2f} Cr"
        if mag >= 1e5:
            return f"{sign}₹{mag / 1e5:,.2f} L"
        return f"{sign}₹{mag:,.2f}"
    if unit == "ratio":
        return f"{value:.2f}"
    return f"{value:,.2f}"


# ── Derived metrics ────────────────────────────────────────────────────────
# Three scored metrics are not in the yfinance payload directly; they are
# computed from fields that are.


def _derive_net_cash(f: dict) -> Optional[float]:
    health = f.get("health") or {}
    cash = _num(health.get("total_cash"))
    debt = _num(health.get("total_debt"))
    if cash is None or debt is None:
        return None
    return cash - debt


def _derive_cash_conversion(f: dict) -> Optional[float]:
    """Operating cash flow ÷ net profit.

    Net profit is not in the payload, so it is reconstructed from the newest
    revenue in ``profit_trend`` when available, else from profit margin applied
    to the newest revenue. Returns None unless a positive profit base exists —
    the ratio is meaningless against a loss.
    """
    health = f.get("health") or {}
    ocf = _num(health.get("operating_cash_flow"))
    if ocf is None:
        return None

    profit_trend = f.get("profit_trend") or []
    net_profit = _num(profit_trend[-1].get("value")) if profit_trend else None

    if net_profit is None:
        revenue_trend = f.get("revenue_trend") or []
        revenue = _num(revenue_trend[-1].get("value")) if revenue_trend else None
        margin = _num(health.get("profit_margin"))
        if revenue is not None and margin is not None:
            net_profit = revenue * margin / 100

    if net_profit is None or net_profit <= 0:
        return None
    return ocf / net_profit


def _derive_revenue_consistency(f: dict) -> Optional[float]:
    """Share of year-on-year steps in which revenue rose (needs 3+ years)."""
    trend = f.get("revenue_trend") or []
    values = [_num(p.get("value")) for p in trend]
    values = [v for v in values if v is not None]
    if len(values) < 3:
        return None
    steps = len(values) - 1
    ups = sum(1 for i in range(1, len(values)) if values[i] > values[i - 1])
    return ups / steps


_DERIVED = {
    "net_cash": _derive_net_cash,
    "cash_conversion": _derive_cash_conversion,
    "revenue_consistency": _derive_revenue_consistency,
}


def _raw_value(key: str, f: dict) -> Optional[float]:
    """Pull a metric out of the fundamentals payload by glossary key."""
    if key in _DERIVED:
        return _DERIVED[key](f)
    for bucket in ("valuation", "health"):
        section = f.get(bucket) or {}
        if key in section:
            return _num(section.get(key))
    return _num(f.get(key))


# ── Public API ─────────────────────────────────────────────────────────────


def _empty_score(reason: str) -> dict:
    return {
        "total": None,
        "grade": None,
        "label": "Not available",
        "confidence": "none",
        "metrics_used": 0,
        "metrics_total": len(SCORED_KEYS),
        "pillars": [],
        "strengths": [],
        "concerns": [],
        "excluded": [],
        "sector_profile": None,
        "note": reason,
    }


def _grade_for(total: float) -> tuple[Optional[str], str]:
    for band in GRADES:
        if total >= band.get("min", 0):
            return band.get("grade"), band.get("label", "")
    return None, ""


def score_fundamentals(fundamentals: dict) -> dict:
    """Grade a fundamentals payload into a 0-100 score with pillar breakdown.

    Returns a dict safe to serialise straight to JSON. Never raises: a missing
    glossary, an error payload or an all-empty metric set each yield a score of
    ``None`` with an explanatory ``note``.
    """
    if not METRICS:
        return _empty_score("Metric glossary unavailable on the server.")
    if not isinstance(fundamentals, dict) or fundamentals.get("error"):
        return _empty_score("No fundamental data to score.")

    profile = _sector_profile(fundamentals.get("sector", ""), fundamentals.get("industry", ""))
    dropped = _dropped_for(profile)

    pillars: list[dict] = []
    used = 0
    excluded: list[str] = []
    strengths: list[dict] = []
    concerns: list[dict] = []
    weighted_total = 0.0
    pillars_with_data = 0

    for pillar_key in PILLAR_ORDER:
        pillar_meta = PILLARS.get(pillar_key, {})
        rows: list[dict] = []
        earned = 0.0
        available_weight = 0.0

        for key in SCORED_KEYS:
            meta = METRICS[key]
            if meta.get("pillar") != pillar_key:
                continue

            if key in dropped:
                excluded.append(key)
                rows.append({
                    "key": key,
                    "label": meta.get("label", key),
                    "value": None,
                    "display": "n/a",
                    "verdict": "excluded",
                    "note": "Not meaningful for this sector",
                })
                continue

            value = _raw_value(key, fundamentals)
            if value is None:
                rows.append({
                    "key": key,
                    "label": meta.get("label", key),
                    "value": None,
                    "display": "—",
                    "verdict": "unknown",
                    "note": "No data available",
                })
                continue

            weight = float(meta.get("weight") or 0)
            verdict, note, points = _grade_value(value, _bands_for(key, profile))
            earned += weight * points
            available_weight += weight
            used += 1

            display = _fmt(value, meta.get("unit", ""))
            rows.append({
                "key": key,
                "label": meta.get("label", key),
                "value": round(value, 4),
                "display": display,
                "verdict": verdict,
                "note": note,
            })

            entry = {"key": key, "text": f"{meta.get('label', key)} {display} — {note}"}
            if verdict == "good":
                strengths.append(entry)
            elif verdict == "bad":
                concerns.append(entry)

        # Rescale to 25 over only the metrics that had data.
        if available_weight > 0:
            pillar_score = round(earned / available_weight * 25, 1)
            pillars_with_data += 1
            weighted_total += pillar_score
        else:
            pillar_score = None

        pillars.append({
            "key": pillar_key,
            "label": pillar_meta.get("label", pillar_key.replace("_", " ").title()),
            "tagline": pillar_meta.get("tagline", ""),
            "score": pillar_score,
            "max": 25,
            "metrics": rows,
        })

    if pillars_with_data == 0:
        return _empty_score("None of the scored metrics had data for this stock.")

    # Scale across the pillars that actually scored, so a stock missing a whole
    # pillar still lands on a 0-100 axis instead of being capped at 75.
    total = round(weighted_total / (pillars_with_data * 25) * 100)
    grade, label = _grade_for(total)

    scored_total = len([k for k in SCORED_KEYS if k not in dropped])
    coverage = used / scored_total if scored_total else 0
    if coverage >= 0.7:
        confidence = "high"
    elif coverage >= 0.5:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "total": total,
        "grade": grade,
        "label": label,
        "confidence": confidence,
        "metrics_used": used,
        "metrics_total": scored_total,
        "pillars": pillars,
        "strengths": [s["text"] for s in strengths[:3]],
        "concerns": [c["text"] for c in concerns[:3]],
        "excluded": excluded,
        "sector_profile": profile,
        "note": None,
    }


def metric_verdicts(score: dict) -> dict[str, dict]:
    """Flatten a score's pillar rows into ``{metric_key: row}``.

    Lets the research page colour each metric row without walking the pillars.
    """
    out: dict[str, dict] = {}
    for pillar in score.get("pillars") or []:
        for row in pillar.get("metrics") or []:
            out[row["key"]] = row
    return out
