"""Jinja2 template loader.

Kept separate from the app factory so routers can import ``templates`` without
importing the application, which would be a circular dependency.
"""
from __future__ import annotations

import logging
import os

from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

_frontend_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)
_static_dir = os.path.join(_frontend_dir, "static")

templates = Jinja2Templates(directory=os.path.join(_frontend_dir, "templates"))


def asset(path: str) -> str:
    """Static URL stamped with the file's mtime, e.g. ``/static/style.css?v=1738...``.

    StaticFiles sends ``last-modified`` and ``etag`` but no ``Cache-Control``.
    With no explicit freshness, browsers fall back to *heuristic* caching —
    roughly 10% of the file's age — so an asset untouched for a week can be
    served from cache for hours after it changes, with no revalidation request.
    That silently hides CSS and JS updates until a hard reload.

    Stamping the URL with the mtime changes the URL whenever the file changes,
    so the browser fetches the new copy immediately and can still cache it hard
    in between. Templates use ``{{ asset('/static/js/research.js') }}``.
    """
    rel = path[len("/static/"):] if path.startswith("/static/") else path.lstrip("/")
    try:
        return f"{path}?v={int(os.path.getmtime(os.path.join(_static_dir, rel)))}"
    except OSError:
        # Missing file (or a path outside static/): fall back to the plain URL
        # rather than breaking the page over a cache hint.
        logger.debug("asset() could not stat %s", path)
        return path


templates.env.globals["asset"] = asset
