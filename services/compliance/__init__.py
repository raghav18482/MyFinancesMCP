"""Mandate and compliance gating.

A hard gate, not advice. ``check()`` returns breaches, and the trade path
refuses to execute when any breach has severity ``block`` — the proposal is
rejected with the specific rule and its source cited, so the answer to "why was
this stopped" is in the response rather than in someone's memory.

Numeric limits live in ``data/compliance_rules.json``, never in Python. They
change, and an audit needs to reconstruct which version was in force on a given
date, which a hardcoded constant cannot support. The shipped file is an
explicitly-labelled template: its limits are placeholders, and the loader warns
when a template ruleset is used.
"""
from services.compliance.rules import (
    Breach,
    ComplianceResult,
    check,
    load_ruleset,
    ruleset_info,
)

__all__ = [
    "Breach",
    "ComplianceResult",
    "check",
    "load_ruleset",
    "ruleset_info",
]
