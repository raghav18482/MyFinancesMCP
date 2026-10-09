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
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from starlette.types import Scope

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


#: Where the React build is served while it runs beside the Jinja pages. Must
#: match ``SPA_BASE`` in ``AgentfolioUI/vite.config.ts``.
SPA_MOUNT_PATH = "/app"


class SinglePageApp(RevalidatingStaticFiles):
    """A built single-page app: real files as-is, every other path as ``index.html``.

    Client-side routes such as ``/app/dashboard`` have no file on disk, so a
    refresh or a pasted link would 404 under plain ``StaticFiles`` — whose
    ``html=True`` only maps a directory to its ``index.html`` and a miss to a
    ``404.html``. Answering those misses with the shell hands the path to the
    React router instead.

    A miss on anything with a file extension stays a 404, so a stale bundle or a
    mistyped asset is reported as missing rather than answered with HTML that
    the browser then fails to parse as JavaScript.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or os.path.splitext(path)[1]:
                raise
        return await super().get_response("index.html", scope)

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if f"{os.sep}assets{os.sep}" in str(full_path):
            # Vite fingerprints everything under assets/, so each URL there names
            # one exact build forever and can be cached for good. index.html keeps
            # the revalidating no-cache, which is what makes a deploy show up.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _spa_dist_dir() -> str | None:
    """The React build to serve, from ``SPA_DIST_DIR``, or ``None`` to serve none.

    Unset is the normal state until the build ships with the server, so it is
    silent. Set but missing is a deploy mistake, so it is logged loudly instead
    of crashing the whole app over the optional half of it.
    """
    raw = os.environ.get("SPA_DIST_DIR", "").strip()
    if not raw:
        return None
    path = raw if os.path.isabs(raw) else os.path.join(_dir, raw)
    if not os.path.isfile(os.path.join(path, "index.html")):
        logger.error("SPA_DIST_DIR=%r has no index.html; the React app is not mounted", raw)
        return None
    return path


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


#: Starlette's own default is 14 days. That was harmless when the cookie only
#: pointed at an in-memory session that died after 8 hours anyway. Now the
#: cookie's ``uid`` can rebuild a trading session from stored credentials, so
#: its lifetime *is* the window in which a stolen cookie can trade. Seven days
#: keeps the convenience the stored credentials are there to provide; shorten it
#: with ``SESSION_COOKIE_MAX_AGE_SEC`` if you want a tighter bound.
_DEFAULT_SESSION_COOKIE_MAX_AGE = 7 * 24 * 3600


def _session_cookie_max_age() -> int:
    raw = os.environ.get("SESSION_COOKIE_MAX_AGE_SEC", "").strip()
    if not raw:
        return _DEFAULT_SESSION_COOKIE_MAX_AGE
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "SESSION_COOKIE_MAX_AGE_SEC=%r is not an integer; using the default", raw
        )
        return _DEFAULT_SESSION_COOKIE_MAX_AGE
    if value <= 0:
        logger.warning("SESSION_COOKIE_MAX_AGE_SEC must be positive; using the default")
        return _DEFAULT_SESSION_COOKIE_MAX_AGE
    return value


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
        max_age=_session_cookie_max_age(),
        same_site="lax",
        https_only=os.environ.get("SESSION_COOKIE_SECURE", "").strip() == "1",
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts())

    app.mount("/static/data", RevalidatingStaticFiles(directory=os.path.join(_dir, "data")), name="data")
    app.mount("/static", RevalidatingStaticFiles(directory=os.path.join(_frontend_dir, "static")), name="static")

    app.include_router(admin_router)
    for router in all_routers:
        app.include_router(router)

    # Last, so no API route or static mount can ever be shadowed by the SPA's
    # catch-all — which matters the day it moves from /app to /.
    spa_dir = _spa_dist_dir()
    if spa_dir:
        app.mount(SPA_MOUNT_PATH, SinglePageApp(directory=spa_dir, html=True), name="spa")
        logger.info("React app mounted at %s from %s", SPA_MOUNT_PATH, spa_dir)

    return app
