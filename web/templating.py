"""Jinja2 template loader.

Kept separate from the app factory so routers can import ``templates`` without
importing the application, which would be a circular dependency.
"""
from __future__ import annotations

import os

from fastapi.templating import Jinja2Templates

_frontend_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)

templates = Jinja2Templates(directory=os.path.join(_frontend_dir, "templates"))

templates = Jinja2Templates(directory=os.path.join(_frontend_dir, "templates"))
