"""Application factory.

Builds the FastAPI instance, installs middleware, mounts static assets and
registers every router. Nothing here knows about individual endpoints; each
domain owns its own module under :mod:`web.routers`.

Middleware order is load-bearing. ``add_middleware`` prepends, so the calls
below leave TrustedHost outermost and Session inside it.
"""
from __future__ import annotations

import logging
import os
import uuid

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from adminApi import admin_router
from web.routers import all_routers

logger = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_frontend_dir = os.path.join(_dir, "frontend")


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that makes browsers check before reusing a cached asset.

    Starlette sends ``last-modified`` and ``etag`` but no ``Cache-Control``.
    With no explicit freshness a browser falls back to *heuristic* caching —
    about 10% of the file's age — so a stylesheet left alone for a week keeps
    being served from cache for hours after it changes, without ever asking us.
    The result is an edit that appears to have no effect until a hard reload.

    ``no-cache`` does not mean "do not cache": it means "cache, but revalidate
    first". Combined with the ETag already being sent, an unchanged file costs
    one small 304 and no body, while a changed one is picked up immediately.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _allowed_hosts() -> list[str]:
    """Hostnames this app will answer to, from ``ALLOWED_HOSTS`` (comma-separated).

    Anything that reads the ``Host`` header — ``request.base_url`` on ``/connect``,
    absolute links, redirects — trusts whatever the client sent unless the host is
    validated here. Set this to your real domain in production, e.g.::

        ALLOWED_HOSTS=myfinancemcp.onrender.com

    Unset means "accept any Host", which is fine locally but leaves Host-header
    reflection open in production, so we log a warning at import time.
    """
    raw = os.environ.get("ALLOWED_HOSTS", "").strip()
    if not raw:
        logger.warning(
            "ALLOWED_HOSTS is not set — accepting any Host header. "
            "Set it to your deployment domain in production."
        )
        return ["*"]
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    # Loopback stays allowed so health checks and local curl keep working.
    for local in ("localhost", "127.0.0.1"):
        if local not in hosts:
            hosts.append(local)
    logger.info("TrustedHostMiddleware active for: %s", ", ".join(hosts))
    return hosts


def create_app() -> FastAPI:
    """Construct the dashboard application."""
    app = FastAPI(
        docs_url="/docs",
        redoc_url="/redoc",
        title="MyFinanceMCP API",
        description="Internal admin and portfolio APIs. Remove docs_url/redoc_url before public deployment.",
        version="1.0.0",
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ.get("SESSION_SECRET", uuid.uuid4().hex),
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts())

    app.mount("/static/data", RevalidatingStaticFiles(directory=os.path.join(_dir, "data")), name="data")
    app.mount("/static", RevalidatingStaticFiles(directory=os.path.join(_frontend_dir, "static")), name="static")

    app.include_router(admin_router)
    for router in all_routers:
        app.include_router(router)

    return app
