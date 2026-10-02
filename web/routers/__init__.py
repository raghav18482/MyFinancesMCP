"""Routers, one module per domain.

``all_routers`` is the single list the app factory iterates. Adding an
endpoint means touching exactly one domain module plus this list.
"""
from __future__ import annotations

from web.routers import (
    agent,
    ai,
    auth,
    briefing,
    feedback,
    market,
    news,
    pages,
    portfolio,
    premium,
    research,
    sectors,
    trading,
)

all_routers = [
    auth.router,
    feedback.router,
    pages.router,
    news.router,
    portfolio.router,
    research.router,
    sectors.router,
    ai.router,
    agent.router,
    premium.router,
    briefing.router,
    trading.router,
    market.router,
]

__all__ = ["all_routers"]
