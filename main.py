"""
Combined entry point: serves the MCP SSE server and the web dashboard
on a single port behind one uvicorn instance.

MCP clients connect to:   /sse   (SSE transport)
Web browsers connect to:  /      (FastAPI dashboard)
"""

import os
import sys
import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

_project_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(_project_dir)
sys.path.insert(0, _project_dir)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from mcp_server import mcp     # noqa: E402  — MCP FastMCP instance
from web_app import web         # noqa: E402  — FastAPI web dashboard
from session_manager import sessions  # noqa: E402
from services.trade_proposals import proposal_store  # noqa: E402


async def _cleanup_loop():
    """Periodically remove expired sessions and stale proposals."""
    while True:
        await asyncio.sleep(600)
        sessions.cleanup_expired()
        proposal_store.cleanup_expired()


@asynccontextmanager
async def _lifespan(_app: Starlette):
    """Starlette 0.37+ removed on_event; use lifespan for startup/shutdown."""
    task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app() -> Starlette:
    mcp_starlette = mcp.sse_app()

    return Starlette(
        lifespan=_lifespan,
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
