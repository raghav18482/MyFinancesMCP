"""
In-process ADK runners for the web UI: one InMemoryRunner per (Angel web session, agent_type).

Broker tools are bound to sid at agent build time; chat state uses ADK session ids stored
in the Starlette session (see web_app).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from google.adk.agents import LlmAgent
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.runners import InMemoryRunner, Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from agents.config import openrouter_api_key
from db import DATABASE_URL

logger = logging.getLogger(__name__)

AgentBuilder = Callable[[str], LlmAgent]

_AGENT_BUILDERS: dict[str, tuple[AgentBuilder, str]] = {}

# Shared, process-wide DB-backed session service for persistent (premium) chats.
# Lazily constructed so a missing/unreachable DB never breaks ephemeral chats.
_db_session_service: DatabaseSessionService | None = None
_db_session_service_failed = False


def _get_db_session_service() -> DatabaseSessionService | None:
    """Return the shared DatabaseSessionService, or None if it can't be built."""
    global _db_session_service, _db_session_service_failed
    if _db_session_service is not None:
        return _db_session_service
    if _db_session_service_failed:
        return None
    try:
        _db_session_service = DatabaseSessionService(db_url=DATABASE_URL)
        logger.info("ADK DatabaseSessionService initialised for persistent chats")
    except Exception:
        _db_session_service_failed = True
        logger.exception("Failed to init DatabaseSessionService; chats will be ephemeral")
        return None
    return _db_session_service


def _register_default_builders() -> None:
    """Lazy-import agent builders to avoid circular imports at module level."""
    if _AGENT_BUILDERS:
        return
    from agents.finance.agent import FINANCE_AGENT_APP_NAME, build_finance_root_agent
    from agents.trading.agent import TRADING_AGENT_APP_NAME, build_trading_root_agent

    _AGENT_BUILDERS["finance"] = (build_finance_root_agent, FINANCE_AGENT_APP_NAME)
    _AGENT_BUILDERS["trading"] = (build_trading_root_agent, TRADING_AGENT_APP_NAME)


def _event_debug_dict(event: Any) -> dict[str, Any]:
    """Small JSON-serializable summary for optional debug responses."""
    out: dict[str, Any] = {
        "author": getattr(event, "author", ""),
        "partial": bool(getattr(event, "partial", False)),
        "is_final_response": False,
        "text_preview": "",
    }
    try:
        out["is_final_response"] = event.is_final_response()
    except Exception:
        pass
    if event.content and event.content.parts:
        texts: list[str] = []
        for p in event.content.parts:
            if getattr(p, "text", None):
                texts.append(p.text)
        joined = "".join(texts)
        out["text_preview"] = joined[:500] + ("…" if len(joined) > 500 else "")
    return out


def _extract_text_from_event(event: Any) -> str:
    if not event.content or not event.content.parts:
        return ""
    return "".join(p.text for p in event.content.parts if getattr(p, "text", None))


def _log_adk_tool_calls(event: Any, angel_sid_prefix: str) -> None:
    """Emit one INFO log line per model-issued tool call (names only)."""
    try:
        calls = event.get_function_calls()
    except Exception:
        calls = []
    if not calls:
        return
    for fc in calls:
        name = getattr(fc, "name", None) or ""
        if name:
            logger.info(
                "ADK tool call: %s (angel_session=%s…)",
                name,
                angel_sid_prefix,
            )


class AdkRunnerRegistry:
    """Caches a runner per (angel_sid, agent_type, persist); serializes runs per key."""

    def __init__(self) -> None:
        self._runners: dict[tuple[str, str, bool], Runner] = {}
        self._locks: dict[tuple[str, str, bool], asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def _lock_for_key(self, key: tuple[str, str, bool]) -> asyncio.Lock:
        async with self._registry_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def _get_or_create_runner(
        self, angel_sid: str, agent_type: str, persist: bool
    ) -> Runner:
        _register_default_builders()
        key = (angel_sid, agent_type, persist)
        if key in self._runners:
            return self._runners[key]

        builder_entry = _AGENT_BUILDERS.get(agent_type)
        if builder_entry is None:
            raise ValueError(f"Unknown agent_type: {agent_type!r}. Available: {list(_AGENT_BUILDERS)}")

        build_fn, app_name = builder_entry
        agent = build_fn(angel_sid)

        db_service = _get_db_session_service() if persist else None
        if db_service is not None:
            runner = Runner(agent=agent, app_name=app_name, session_service=db_service)
        else:
            runner = InMemoryRunner(agent=agent, app_name=app_name)
        self._runners[key] = runner
        logger.info(
            "ADK runner created: type=%s persist=%s session_prefix=%s",
            agent_type, bool(db_service is not None), angel_sid[:8],
        )
        return runner

    async def _ensure_adk_session(
        self,
        runner: InMemoryRunner,
        *,
        user_id: str,
        adk_session_id: str,
    ) -> None:
        existing = await runner.session_service.get_session(
            app_name=runner.app_name,
            user_id=user_id,
            session_id=adk_session_id,
        )
        if existing is not None:
            return
        try:
            await runner.session_service.create_session(
                app_name=runner.app_name,
                user_id=user_id,
                session_id=adk_session_id,
            )
        except AlreadyExistsError:
            pass

    async def chat(
        self,
        *,
        angel_sid: str,
        adk_session_id: str,
        message: str,
        agent_type: str = "finance",
        debug: bool = False,
        user_id: str | None = None,
        persist: bool = False,
    ) -> dict[str, Any]:
        if not openrouter_api_key():
            raise ValueError(
                "OPENROUTER_API_KEY is not set. Configure it on the server for the ADK agent."
            )

        text = message.strip()
        if not text:
            raise ValueError("Message cannot be empty.")

        user_id = user_id or f"web-{angel_sid}"
        key = (angel_sid, agent_type, persist)
        sid_lock = await self._lock_for_key(key)
        async with sid_lock:
            runner = await self._get_or_create_runner(angel_sid, agent_type, persist)
            await self._ensure_adk_session(
                runner, user_id=user_id, adk_session_id=adk_session_id
            )

            content = types.Content(role="user", parts=[types.Part(text=text)])
            final_text = ""
            fallback_text = ""
            debug_events: list[dict[str, Any]] = []

            sid_prefix = angel_sid[:8] if len(angel_sid) >= 8 else angel_sid
            async for event in runner.run_async(
                user_id=user_id,
                session_id=adk_session_id,
                new_message=content,
            ):
                _log_adk_tool_calls(event, sid_prefix)
                if debug:
                    debug_events.append(_event_debug_dict(event))

                if getattr(event, "author", None) and event.author != "user":
                    piece = _extract_text_from_event(event)
                    if piece:
                        fallback_text = piece

                try:
                    if event.is_final_response():
                        piece = _extract_text_from_event(event)
                        if piece:
                            final_text = piece
                except Exception:
                    pass

            reply = (final_text or fallback_text).strip()
            if not reply:
                reply = "(No text response from the agent.)"

            result: dict[str, Any] = {"reply": reply}
            if debug:
                result["events"] = debug_events
            return result

    async def get_messages(
        self, *, user_id: str, adk_session_id: str, app_name: str
    ) -> list[dict[str, str]]:
        """Rebuild the visible {role, text} message list for a persisted session.

        Reads events directly from the shared DatabaseSessionService so the UI
        can render past conversations without a separate message store.
        """
        db_service = _get_db_session_service()
        if db_service is None:
            return []
        session = await db_service.get_session(
            app_name=app_name, user_id=user_id, session_id=adk_session_id
        )
        if session is None:
            return []

        messages: list[dict[str, str]] = []
        for event in getattr(session, "events", []) or []:
            if getattr(event, "partial", False):
                continue
            text = _extract_text_from_event(event)
            if not text or not text.strip():
                continue
            author = getattr(event, "author", "") or ""
            role = "user" if author == "user" else "assistant"
            messages.append({"role": role, "text": text.strip()})
        return messages

    async def delete_session(
        self, *, user_id: str, adk_session_id: str, app_name: str
    ) -> None:
        """Best-effort delete of a persisted ADK session."""
        db_service = _get_db_session_service()
        if db_service is None:
            return
        try:
            await db_service.delete_session(
                app_name=app_name, user_id=user_id, session_id=adk_session_id
            )
        except Exception:
            logger.warning("Failed to delete ADK session %s", adk_session_id[:8])


def app_name_for(agent_type: str) -> str:
    """Resolve the ADK app_name for an agent type (for session lookups)."""
    _register_default_builders()
    entry = _AGENT_BUILDERS.get((agent_type or "finance").strip().lower())
    if entry is None:
        entry = _AGENT_BUILDERS.get("finance")
    return entry[1]


registry = AdkRunnerRegistry()
