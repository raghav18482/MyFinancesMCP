"""JSON twins of the login, enrollment and logout forms, for the React app.

The flows live in :mod:`web.auth_flow`; this module only translates between
JSON and :class:`~web.auth_flow.AuthFlowError`. Every error body has the shape
``{"error": str, "field"?: str}`` — the same ``error`` key every other ``/api``
endpoint uses — so the client needs one error parser. ``field`` names the input
to highlight when the failure belongs to exactly one.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from db.passwords import MIN_PASSWORD_CHARS
from web import auth_flow, country_codes

logger = logging.getLogger(__name__)

router = APIRouter()


async def _json_object(request: Request) -> dict | None:
    """The request body as a dict, or ``None`` if it is not a JSON object."""
    try:
        body = await request.json()
    except ValueError:  # JSONDecodeError and UnicodeDecodeError both subclass it
        return None
    return body if isinstance(body, dict) else None


def _text(body: dict, key: str) -> str:
    """A string field, empty when absent. Not stripped: passwords keep their spaces."""
    value = body.get(key)
    return value if isinstance(value, str) else ""


def _flow_error(e: auth_flow.AuthFlowError) -> JSONResponse:
    content: dict[str, str] = {"error": e.message}
    if e.field:
        content["field"] = e.field
    headers = {"Retry-After": str(e.retry_after_sec)} if e.retry_after_sec else None
    return JSONResponse(content, status_code=e.status_code, headers=headers)


_INVALID_BODY = {"error": "Invalid JSON body"}


@router.get("/api/auth/config")
async def api_auth_config():
    """What the login, enroll and premium forms need before they can render."""
    return JSONResponse({
        "countries": country_codes.options(),
        "default_dial_code": country_codes.DEFAULT_DIAL_CODE,
        "min_password_chars": MIN_PASSWORD_CHARS,
    })


@router.post("/api/auth/login")
async def api_auth_login(request: Request):
    body = await _json_object(request)
    if body is None:
        return JSONResponse(_INVALID_BODY, status_code=400)

    try:
        client = auth_flow.login(
            request,
            _text(body, "country_code"),
            _text(body, "whatsapp_number"),
            _text(body, "app_password"),
        )
    except auth_flow.AuthFlowError as e:
        return _flow_error(e)

    return JSONResponse({"ok": True, "client_id": client.client_id})


@router.post("/api/auth/enroll")
async def api_auth_enroll(request: Request):
    body = await _json_object(request)
    if body is None:
        return JSONResponse(_INVALID_BODY, status_code=400)

    try:
        view = auth_flow.enroll(
            request,
            api_key=_text(body, "api_key"),
            client_id=_text(body, "client_id"),
            password=_text(body, "password"),
            totp_secret=_text(body, "totp_secret"),
            country_code=_text(body, "country_code"),
            whatsapp_number=_text(body, "whatsapp_number"),
            app_password=_text(body, "app_password"),
            app_password_confirm=_text(body, "app_password_confirm"),
        )
    except auth_flow.AuthFlowError as e:
        return _flow_error(e)

    return JSONResponse({"ok": True, "client_id": view["angel_client_id"]})


@router.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    auth_flow.logout(request)
    return JSONResponse({"ok": True})
