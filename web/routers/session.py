"""Who is signed in, for the React app.

The server-rendered pages learn this from :func:`web.dependencies.template_context`
on every render. A single-page app renders once, so it asks here instead, and
uses the answer for route guards, the premium gate, and the passphrase that
unlocks the OpenRouter key it keeps in ``localStorage``.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from web.dependencies import current_client, registered_user_for_session

router = APIRouter()

# Per-user and auth-bearing: never let a browser or proxy reuse one.
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/api/session/bootstrap")
async def api_session_bootstrap(request: Request):
    """The signed-in user, or ``{"authenticated": false}``.

    Anonymous is a 200, not a 401: it is an answer, not a failure, and the
    client needs to tell it apart from a request that did fail.

    Like every logged-in page, this goes through :func:`current_client`, so a
    cookie whose broker session lapsed is reconnected here rather than bounced.
    """
    client = current_client(request)
    if client is None:
        return JSONResponse({"authenticated": False}, headers=_NO_STORE)

    user = registered_user_for_session(client)
    premium = bool(user and user.is_active)
    return JSONResponse(
        {
            "authenticated": True,
            "client_id": client.client_id,
            "premium": {
                "registered": premium,
                "whatsapp_number": user.whatsapp_number if premium else None,
            },
        },
        headers=_NO_STORE,
    )
