"""Who is signed in, for the React app.

The server-rendered pages learn this from :func:`web.dependencies.template_context`
on every render. A single-page app renders once, so it asks here instead, and
uses the answer for route guards, the brokerage and premium gates, and the
passphrase that unlocks the OpenRouter key it keeps in ``localStorage``.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from adminApi import service as admin_service
from web.auth_flow import account_payload
from web.dependencies import current_app_user

router = APIRouter()

# Per-user and auth-bearing: never let a browser or proxy reuse one.
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/api/session/bootstrap")
async def api_session_bootstrap(request: Request):
    """The signed-in user, or ``{"authenticated": false}``.

    Anonymous is a 200, not a 401: it is an answer, not a failure, and the
    client needs to tell it apart from a request that did fail.

    Signed in means a valid cookie for an active user — with or without a
    broker. Nothing here contacts Angel One, so the app loads even when the
    broker is down; ``brokerage_status`` says whether brokerage pages will work.
    """
    user = current_app_user(request)
    if user is None:
        return JSONResponse({"authenticated": False}, headers=_NO_STORE)

    connected = admin_service.is_connected(user)
    return JSONResponse(
        {
            "authenticated": True,
            **account_payload(user),
            # Premium (WhatsApp briefings) runs on the broker link, so it is
            # exactly "connected" now that every signed-in user has a row.
            "premium": {
                "registered": connected,
                "whatsapp_number": user.whatsapp_number if connected else None,
            },
        },
        headers=_NO_STORE,
    )
