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

# Before anything else is imported, so every handler carries the redaction filter.
from services.logging_config import (  # noqa: E402
    after_uvicorn_configured,
    configure_logging,
    uvicorn_log_config,
    verify_logging,
)

configure_logging()
logger = logging.getLogger(__name__)

from mcp_server import ClientIPMiddleware, auth_settings, mcp  # noqa: E402  — MCP FastMCP instance
from web_app import web         # noqa: E402  — FastAPI web dashboard
from web import security_config  # noqa: E402
from web.session_cleanup import release_session_resources  # noqa: E402
from session_manager import sessions  # noqa: E402
from services.trade_proposals import proposal_store  # noqa: E402
from services.realtime_feed import feed_relay  # noqa: E402


CLEANUP_INTERVAL_SEC = 120
#: The slower housekeeping runs every this many cleanup ticks (10 minutes).
SLOW_TICKS = 5


async def _cleanup_loop():
    """Release expired or replaced broker sessions every two minutes; slower
    housekeeping (logging check, stale proposals, purges) every ten.

    Session resources (ADK runners, feeds, caches) are not thread-safe, so they
    are released here, on the event loop.
    """
    from web import broker_sessions

    tick = 0
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)
        tick += 1
        for sid in sessions.cleanup_expired():
            release_session_resources(sid)
        try:
            broker_sessions.get_runtime().purge_memo()
        except Exception:
            logger.exception("Broker memo purge failed; will retry")
        if tick % SLOW_TICKS:
            continue
        try:
            verify_logging()
        except Exception:
            logger.exception("Logging verification failed; will retry")
        proposal_store.cleanup_expired()
        try:
            from services.oauth_provider import purge_expired

            await asyncio.to_thread(purge_expired)
        except Exception:
            logger.exception("OAuth purge failed; will retry")
        try:
            from services.feedback import purge_old_submissions

            # A no-op until FEEDBACK_RETENTION_DAYS is set.
            await asyncio.to_thread(purge_old_submissions)
        except Exception:
            logger.exception("Feedback retention purge failed; will retry")


async def _instance_heartbeat(monitor):
    """Report this process every minute so a second instance is noticed."""
    from services.instance_registry import BEAT_SECONDS

    failures = 0
    while True:
        try:
            await asyncio.to_thread(monitor.beat)
            failures = 0
        except Exception as e:
            failures += 1
            # Full detail once; after that a line a minute is enough.
            if failures == 1:
                logger.exception("Instance heartbeat failed; will retry")
            else:
                logger.warning("Instance heartbeat still failing (%s, %d times)",
                               type(e).__name__, failures)
        await asyncio.sleep(BEAT_SECONDS)


@asynccontextmanager
async def _lifespan(_app: Starlette):
    """Starlette 0.37+ removed on_event; use lifespan for startup/shutdown."""
    # uvicorn has applied its log_config by now, and dictConfig re-enables every
    # logger, SmartAPI's included. Re-establish redaction before the first
    # request is served.
    after_uvicorn_configured()

    try:
        from db import init_db

        init_db()
    except Exception as e:
        # Code now depends on the tables and columns init_db adds, so serving
        # on a stale schema would fail at the first request instead. In
        # production, refuse to start; Render keeps the previous instance up.
        if security_config.is_production():
            logger.exception("init_db failed; refusing to start")
            raise
        logger.warning("init_db skipped: %s", e)

    scheduler_enabled = os.environ.get("SCHEDULER_ENABLED") == "1"
    if scheduler_enabled:
        try:
            from services.schedular.runner import start as start_scheduler

            start_scheduler()
        except Exception:
            logger.exception("scheduler failed to start; continuing without it")
            scheduler_enabled = False

    from db import engine as db_engine
    from services.instance_registry import InstanceMonitor
    from web import broker_sessions

    # Bound to this loop; logins run in its worker threads (web/broker_sessions.py).
    runtime = broker_sessions.get_runtime()

    monitor = InstanceMonitor(db_engine)
    logger.info("instance %s starting", monitor.instance_id)
    heartbeat = asyncio.create_task(_instance_heartbeat(monitor))

    task = asyncio.create_task(_cleanup_loop())
    try:
        # The Streamable HTTP transport's task group must run for the app's lifetime.
        async with mcp.session_manager.run():
            yield
    finally:
        await runtime.stop()
        heartbeat.cancel()
        try:
            await asyncio.to_thread(monitor.stop)
        except Exception:
            logger.warning("Could not remove instance row %s", monitor.instance_id)
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
        # Our handlers, with the redaction filter, instead of uvicorn's own.
        log_config=uvicorn_log_config(),
    )
