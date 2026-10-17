"""Releasing everything that hangs off a web session id when the session ends.

Called on logout, on a rejected (stale or mismatched) session, and by the
periodic expiry sweep in ``main.py``, so per-session state never outlives the
session it belongs to.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def release_session_resources(sid: str | None) -> None:
    """Stop the feed and drop runners and caches for ``sid``. Best effort, never raises."""
    if not sid:
        return
    from services.adk_runner_registry import registry
    from services.portfolio_service import forget_portfolio_cache
    from services.realtime_feed import feed_relay
    from services.risk_profile import risk_profiles

    for name, fn in (
        ("feed", lambda: feed_relay.stop_feed(sid)),
        ("adk runners", lambda: registry.evict(sid)),
        ("portfolio cache", lambda: forget_portfolio_cache(sid)),
        ("risk profile cache", lambda: risk_profiles.remove(sid)),
    ):
        try:
            fn()
        except Exception:
            logger.exception("Releasing %s for session %s failed", name, sid[:8])
