"""
In-process ADK runners for the web UI: one InMemoryRunner per Angel web session (sid).

Broker tools are bound to sid at agent build time; chat state uses ADK session ids stored
in the Starlette session (see web_app).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.runners import InMemoryRunner
from google.genai import types

from agents.config import openrouter_api_key
from agents.finance.agent import FINANCE_AGENT_APP_NAME, build_finance_root_agent

logger = logging.getLogger(__name__)


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
    """Caches InMemoryRunner per angel_sid; serializes runs per sid with asyncio.Lock."""

    def __init__(self) -> None:
        self._runners: dict[str, InMemoryRunner] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def _lock_for_sid(self, angel_sid: str) -> asyncio.Lock:
        async with self._registry_lock:
            if angel_sid not in self._locks:
                self._locks[angel_sid] = asyncio.Lock()
            return self._locks[angel_sid]

    async def _get_or_create_runner(self, angel_sid: str) -> InMemoryRunner:
        if angel_sid in self._runners:
            return self._runners[angel_sid]
        agent = build_finance_root_agent(angel_sid)
        runner = InMemoryRunner(agent=agent, app_name=FINANCE_AGENT_APP_NAME)
        self._runners[angel_sid] = runner
        logger.info("ADK runner created for session prefix=%s", angel_sid[:8])
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
        debug: bool = False,
    ) -> dict[str, Any]:
        if not openrouter_api_key():
            raise ValueError(
                "OPENROUTER_API_KEY is not set. Configure it on the server for the ADK agent."
            )

        text = message.strip()
        if not text:
            raise ValueError("Message cannot be empty.")

        user_id = f"web-{angel_sid}"
        sid_lock = await self._lock_for_sid(angel_sid)
        async with sid_lock:
            runner = await self._get_or_create_runner(angel_sid)
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


registry = AdkRunnerRegistry()
