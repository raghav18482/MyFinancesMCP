"""Premium registration and status."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse



from adminApi import service as admin_service

from web.dependencies import (
    current_client,
    registered_user_for_session,
)

logger = logging.getLogger(__name__)

router = APIRouter()



# ── Premium registration API ───────────────────────────────────────────────


@router.get("/api/premium/status")
async def api_premium_status(request: Request):
    client = current_client(request)
    if not client:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = registered_user_for_session(client)
    if user and user.is_active:
        return JSONResponse({
            "registered": True,
            "whatsapp_number": user.whatsapp_number,
            "user_id": user.id,
        })
    return JSONResponse({"registered": False})



@router.post("/api/premium/register")
async def api_premium_register(request: Request):
    """Register the logged-in browser session for premium (WhatsApp briefings).

    Credentials are taken from the in-memory ``AngelOneClient`` for this session
    and handed straight to :mod:`adminApi.service`, which encrypts them and
    writes the row. This deliberately does **not** go through the HTTP admin API:
    an earlier version POSTed to ``f"{request.base_url}/api/admin/users"``, and
    because ``base_url`` is derived from the client's ``Host`` header, a request
    with ``Host: attacker.tld`` made the server send the user's plaintext PIN,
    TOTP secret and ``ADMIN_API_KEY`` to that host. Keep this call in-process.
    """
    client = current_client(request)
    if not client:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    whatsapp_number = (body.get("whatsapp_number") or "").strip()
    if not whatsapp_number.startswith("+"):
        return JSONResponse(
            {"error": "whatsapp_number must start with + and country code"},
            status_code=400,
        )

    existing = registered_user_for_session(client)
    if existing and existing.is_active:
        return JSONResponse(
            {"ok": False, "error": "You are already registered for premium."},
            status_code=409,
        )

    try:
        if existing is not None:
            # Dormant row for this client_id — reactivate it with fresh credentials.
            view = admin_service.update_user(
                existing.id,
                whatsapp_number=whatsapp_number,
                angel_api_key=client.api_key,
                angel_password=client.password,
                angel_totp_secret=client.totp_secret,
                is_active=True,
            )
        else:
            view = admin_service.create_user(
                whatsapp_number=whatsapp_number,
                angel_api_key=client.api_key,
                angel_client_id=client.client_id,
                angel_password=client.password,
                angel_totp_secret=client.totp_secret,
            )
    except admin_service.UserAlreadyExists:
        return JSONResponse(
            {"ok": False, "error": "You are already registered for premium."},
            status_code=409,
        )
    except admin_service.UserNotFound:
        logger.warning("Premium registration: user row vanished mid-request")
        return JSONResponse(
            {"ok": False, "error": "Registration failed. Please try again."},
            status_code=409,
        )
    except Exception:
        # Never surface the exception text: it can carry credential material.
        logger.exception("Premium registration failed")
        return JSONResponse(
            {"ok": False, "error": "Registration failed. Please try again."},
            status_code=500,
        )

    return JSONResponse({"ok": True, "user": admin_service.jsonable(view)})
