"""Request-scoped Angel One client and user key for LangGraph tools."""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

# Populated per HTTP request before graph.invoke
angel_client_var: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "angel_client", default=None
)
user_key_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "user_key", default="anonymous"
)


def get_angel_client():
    c = angel_client_var.get()
    if c is None:
        raise RuntimeError("No broker client in context; user must be logged in.")
    return c


def get_user_key() -> str:
    return user_key_var.get() or "anonymous"
