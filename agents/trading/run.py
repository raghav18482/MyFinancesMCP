"""
Interactive CLI for the trading ADK agent.

Usage (from repo root)::

    python -m agents.trading.run
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner, print_event
from google.genai import types

from agents.trading.agent import TRADING_AGENT_APP_NAME, build_trading_root_agent
from session_manager import sessions

logger = logging.getLogger(__name__)


def _angel_creds() -> tuple[str, str, str, str]:
    load_dotenv()
    key = os.environ.get("ANGELONE_API_KEY") or os.environ.get("ANGEL_API_KEY") or ""
    cid = os.environ.get("ANGELONE_CLIENT_ID") or os.environ.get("ANGEL_CLIENT_ID") or ""
    pwd = os.environ.get("ANGELONE_PASSWORD") or os.environ.get("ANGEL_PASSWORD") or ""
    totp = os.environ.get("ANGELONE_TOTP_SECRET") or os.environ.get("ANGEL_TOTP_SECRET") or ""
    missing = [n for n, v in [("api_key", key), ("client_id", cid), ("password", pwd), ("totp", totp)] if not v]
    if missing:
        raise SystemExit(
            "Missing Angel One credentials: " + ", ".join(missing)
        )
    return key.strip(), cid.strip(), pwd.strip(), totp.strip()


async def _chat_loop(runner: InMemoryRunner, user_id: str, chat_session_id: str) -> None:
    print("Trading agent ready. Type a message (empty line to exit).", flush=True)
    while True:
        try:
            line = await asyncio.to_thread(sys.stdin.readline)
        except (KeyboardInterrupt, EOFError):
            print()
            break
        text = (line or "").strip()
        if not text:
            break
        content = types.Content(role="user", parts=[types.Part(text=text)])
        async for event in runner.run_async(
            user_id=user_id,
            session_id=chat_session_id,
            new_message=content,
        ):
            print_event(event)


async def async_main() -> None:
    logging.basicConfig(level=logging.INFO)
    load_dotenv()

    angel_sid = (os.environ.get("ADK_ANGEL_SESSION_ID") or "").strip() or str(uuid.uuid4())
    os.environ["ADK_ANGEL_SESSION_ID"] = angel_sid

    api_key, client_id, password, totp_secret = _angel_creds()
    sessions.create_session(angel_sid, api_key, client_id, password, totp_secret)
    print(f"Angel session id: {angel_sid}", flush=True)

    agent = build_trading_root_agent(angel_sid)
    runner = InMemoryRunner(agent=agent, app_name=TRADING_AGENT_APP_NAME)

    user_id = "local-user"
    chat_session_id = "cli-trading-chat"
    await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id=user_id,
        session_id=chat_session_id,
    )

    await _chat_loop(runner, user_id, chat_session_id)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
