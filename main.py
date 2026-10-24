"""
Combined entry point: serves the read-only MCP server and the web dashboard
on a single port behind one uvicorn instance.

MCP clients connect to:   /mcp/mcp   (Streamable HTTP, ``Authorization: Bearer <token>``)
Web browsers connect to:  /          (FastAPI dashboard)
OAuth 2.1 for MCP:        /.well-known/oauth-*, /oauth/*  (served by the dashboard app,
                          see ``web/routers/oauth.py``: they must sit at the domain root)

Both sit behind one ``TrustedHostMiddleware``, so the MCP mount gets the same
Host-header check as the web app.
"""

import os
import sys
import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.routing import Mount

_project_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(_project_dir)
sys.path.insert(0, _project_dir)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_project_dir, ".env"))
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from mcp_server import ClientIPMiddleware, auth_settings, mcp  # noqa: E402  — MCP FastMCP instance
from web_app import web         # noqa: E402  — FastAPI web dashboard
from web import security_config  # noqa: E402
from web.session_cleanup import release_session_resources  # noqa: E402
from session_manager import sessions  # noqa: E402
from services.trade_proposals import proposal_store  # noqa: E402
from services.realtime_feed import feed_relay  # noqa: E402


async def _cleanup_loop():
    """Periodically remove expired sessions, their per-session state, and stale proposals."""
    while True:
        await asyncio.sleep(600)
        for sid in sessions.cleanup_expired():
            release_session_resources(sid)
        proposal_store.cleanup_expired()
        try:
            from services.oauth_provider import purge_expired

            await asyncio.to_thread(purge_expired)
        except Exception:
            logger.exception("OAuth purge failed; will retry")


@asynccontextmanager
async def _lifespan(_app: Starlette):
    """Starlette 0.37+ removed on_event; use lifespan for startup/shutdown."""
    try:
        from db import init_db

        init_db()
    except Exception as e:
        logger.warning("init_db skipped: %s", e)

    scheduler_enabled = os.environ.get("SCHEDULER_ENABLED") == "1"
    if scheduler_enabled:
        try:
            from services.schedular.runner import start as start_scheduler

            start_scheduler()
        except Exception:
            logger.exception("scheduler failed to start; continuing without it")
            scheduler_enabled = False

    task = asyncio.create_task(_cleanup_loop())
    try:
        # The Streamable HTTP transport's task group must run for the app's lifetime.
        async with mcp.session_manager.run():
            yield
    finally:
        if scheduler_enabled:
            try:
                from services.schedular.runner import stop as stop_scheduler

                stop_scheduler()
            except Exception:
                logger.exception("scheduler failed to stop cleanly")
        feed_relay.stop_all()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app() -> Starlette:
    # Resource URL and issuer follow PUBLIC_BASE_URL as it is now.
    mcp.settings.auth = auth_settings()
    # ``streamable_http_app`` serves ``/mcp``; mounted at ``/mcp`` the endpoint is ``/mcp/mcp``.
    mcp_starlette = ClientIPMiddleware(mcp.streamable_http_app())

    return Starlette(
        lifespan=_lifespan,
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=security_config.allowed_hosts())],
        routes=[
            Mount("/mcp", app=mcp_starlette),
            Mount("/", app=web),
        ],
    )


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
