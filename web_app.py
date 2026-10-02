"""Composition root for the dashboard application.

The application itself is assembled in :mod:`web.app`; this module exists so
``main.py`` and the deployment entry points keep a stable import path.

Looking for an endpoint? It lives in ``web/routers/<domain>.py``. The list of
registered routers is in ``web/routers/__init__.py``.
"""
from __future__ import annotations

from web.app import create_app

web = create_app()

__all__ = ["web", "create_app"]
