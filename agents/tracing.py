"""
LangSmith tracing for ADK agents.

Off unless LANGSMITH_TRACING=true and LANGSMITH_API_KEY are set. When on, every
Runner.run_async (web chat, CLI, daily briefing) is traced: agent steps, LLM
calls, tool calls, latency and tokens. Traces carry the full tool inputs and
outputs, including the user's holdings and funds.

Env: LANGSMITH_TRACING, LANGSMITH_API_KEY, LANGSMITH_ENDPOINT,
LANGSMITH_AGENT_ID, LANGSMITH_AGENT_ENVIRONMENT (local / production).
"""
from __future__ import annotations

import logging
import os

from agents import config  # noqa: F401  (loads .env before the env is read)

logger = logging.getLogger(__name__)

_configured = False


def langsmith_enabled() -> bool:
    flag = (os.environ.get("LANGSMITH_TRACING") or "").strip().lower()
    key = (os.environ.get("LANGSMITH_API_KEY") or "").strip()
    return flag in ("1", "true", "yes") and bool(key)


def configure_tracing() -> None:
    """Patch ADK for LangSmith once per process. Never raises: tracing must not break chat."""
    global _configured
    if _configured:
        return
    _configured = True
    if not langsmith_enabled():
        return
    try:
        from langsmith.integrations.google_adk import configure_google_adk

        environment = os.environ.get("LANGSMITH_AGENT_ENVIRONMENT", "local").strip()
        ok = configure_google_adk(
            metadata={"environment": environment},
            tags=[environment],
        )
    except Exception:
        logger.exception("LangSmith tracing setup failed; continuing without tracing")
        return
    if ok:
        logger.info("LangSmith tracing enabled for ADK agents")
    else:
        logger.warning("LangSmith tracing could not patch google-adk; continuing without tracing")
