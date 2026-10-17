"""Personal access tokens for the read-only MCP endpoint (create, list, revoke).

These are custom bearer tokens, not OAuth — see :mod:`services.mcp_tokens`.
Every route acts only on the signed-in user's own tokens. Creating one needs
the app password again, so a stolen session cookie cannot be turned into a
long-lived token.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from db.passwords import verify_password
from services import login_throttle, mcp_tokens
from web.dependencies import client_ip, current_user

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/api/mcp/tokens")
async def api_mcp_tokens_list(request: Request):
    user = current_user(request)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401, headers=_NO_STORE)
    return JSONResponse({"tokens": mcp_tokens.list_for_user(user.id)}, headers=_NO_STORE)


@router.post("/api/mcp/tokens")
async def api_mcp_tokens_create(request: Request):
    """Create a token. The raw value is in this response and nowhere else, ever."""
    user = current_user(request)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401, headers=_NO_STORE)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400, headers=_NO_STORE)

    identifier = f"mcp-token:{user.id}"
    ip = client_ip(request)
    try:
        login_throttle.check(identifier, ip)
    except login_throttle.TooManyAttempts as e:
        return JSONResponse({"error": str(e)}, status_code=429,
                            headers={**_NO_STORE, "Retry-After": str(e.retry_after_sec)})

    password = body.get("app_password")
    if not verify_password(password if isinstance(password, str) else "", user.app_password_hash):
        return JSONResponse({"error": "That password is not correct.", "field": "app_password"},
                            status_code=401, headers=_NO_STORE)
    login_throttle.clear(identifier, ip)

    try:
        raw, view = mcp_tokens.issue(
            user.id,
            str(body.get("name") or "MCP client"),
            ttl_days=body.get("ttl_days", mcp_tokens.DEFAULT_TTL_DAYS),
            client_ip=ip,
        )
    except mcp_tokens.TooManyRequests as e:
        return JSONResponse({"error": str(e)}, status_code=429,
                            headers={**_NO_STORE, "Retry-After": str(e.retry_after_sec)})
    except mcp_tokens.TokenError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status_code, headers=_NO_STORE)

    return JSONResponse(
        {
            "token": raw,
            "token_info": view,
            "note": "Copy this token now. It is not stored and cannot be shown again.",
        },
        status_code=201,
        headers=_NO_STORE,
    )


@router.delete("/api/mcp/tokens/{token_id}")
async def api_mcp_tokens_revoke(request: Request, token_id: int):
    user = current_user(request)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401, headers=_NO_STORE)
    if not mcp_tokens.revoke(user.id, token_id):
        # Someone else's token and a token that does not exist look the same.
        return JSONResponse({"error": "Token not found"}, status_code=404, headers=_NO_STORE)
    return JSONResponse({"ok": True}, headers=_NO_STORE)
