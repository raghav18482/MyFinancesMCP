"""Trading ADK agent (proposal-based, risk-aware)."""

from __future__ import annotations

import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
_rr = str(_repo_root)
if _rr not in sys.path:
    sys.path.insert(0, _rr)
