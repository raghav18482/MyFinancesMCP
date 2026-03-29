"""Finance ADK agent (Angel One + research tools)."""

from __future__ import annotations

import sys
from pathlib import Path

# ``adk web <agents_dir>`` puts only ``agents/`` on sys.path; repo-root imports need the project root.
_repo_root = Path(__file__).resolve().parents[2]
_rr = str(_repo_root)
if _rr not in sys.path:
    sys.path.insert(0, _rr)
