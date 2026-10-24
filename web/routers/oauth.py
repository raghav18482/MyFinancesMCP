"""OAuth 2.1 endpoints for the MCP server, and the sign-in + consent page.

The protocol endpoints are the MCP SDK's own handlers, wired to
:class:`services.oauth_provider.McpOAuthProvider`. They are registered here, on
the root web app, rather than by FastMCP: FastMCP is mounted at ``/mcp``, so
the routes it would add for itself would be served under ``/mcp/...`` and
clients would never find the well-known documents.

- ``/.well-known/oauth-protected-resource[/mcp/mcp]`` — RFC 9728, for the MCP endpoint
- ``/.well-known/oauth-authorization-server`` — RFC 8414
- ``/oauth/register`` — Dynamic Client Registration (RFC 7591)
- ``/oauth/authorize`` — validates the request, then sends the browser to
- ``/oauth/consent`` — sign in (WhatsApp number + app password, throttled like
  ``/login``) and approve read-only access
- ``/oauth/token``, ``/oauth/revoke``

``/api/oauth/connections`` lets a signed-in user see and revoke connected apps.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route, request_response

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.streamable_http import MCP_PROTOCOL_VERSION_HEADER
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE, RequestBodyLimitMiddleware

from services import login_throttle
from services import oauth_provider as oauth
from services.account_deletion import password_matches, user_by_number
from web import country_codes, security_config
from web.dependencies import client_ip, current_user
from web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

provider = oauth.McpOAuthProvider(security_config.public_base_url)
_authenticator = oauth.HashedSecretClientAuthenticator(provider)
_registration = RegistrationHandler(
    provider,
    options=ClientRegistrationOptions(
        enabled=True, valid_scopes=oauth.SUPPORTED_SCOPES, default_scopes=oauth.SUPPORTED_SCOPES
    ),
)

_NO_STORE = {"Cache-Control": "no-store"}
_cors_get = ["GET", "OPTIONS"]
_cors_post = ["POST", "OPTIONS"]
# The consent page takes a password: never framed, never cached, and its URL
# (which carries the sealed request) is not sent to third-party font/CDN hosts.
_CONSENT_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


def _cors(app, methods: list[str]):
    # Same policy as the SDK's own auth routes: browser-based clients (MCP
    # Inspector) may call the metadata, token, registration and revocation
    # endpoints. None of them uses cookies, so a wildcard origin grants nothing.
    return CORSMiddleware(app=app, allow_origins="*", allow_methods=methods,
                          allow_headers=[MCP_PROTOCOL_VERSION_HEADER])


def _limited(app):
    return RequestBodyLimitMiddleware(app, DEFAULT_MAX_REQUEST_BODY_SIZE)


def _get(handler):
    return _cors(request_response(handler), _cors_get)


def _post(handler):
    return _cors(_limited(request_response(handler)), _cors_post)


async def _as_metadata(request: Request):
    return PydanticJSONResponse(
        content=oauth.authorization_server_metadata(provider.urls),
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _pr_metadata(request: Request):
    return PydanticJSONResponse(
        content=oauth.protected_resource_metadata(provider.urls),
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _register(request: Request):
    retry = oauth.check_registration_rate(client_ip(request))
    if retry is not None:
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "Too many registrations; try later"},
            status_code=429, headers={**_NO_STORE, "Retry-After": str(retry)},
        )
    return await _registration.handle(request)


router.routes.extend([
    Route("/.well-known/oauth-authorization-server", endpoint=_get(_as_metadata), methods=_cors_get),
    Route("/.well-known/oauth-protected-resource", endpoint=_get(_pr_metadata), methods=_cors_get),
    Route("/.well-known/oauth-protected-resource" + oauth.MCP_RESOURCE_PATH,
          endpoint=_get(_pr_metadata), methods=_cors_get),
    # No CORS on authorize: browsers are redirected to it, scripts don't call it.
    Route(oauth.AUTHORIZE_PATH,
          endpoint=_limited(request_response(AuthorizationHandler(provider).handle)),
          methods=["GET", "POST"]),
    Route(oauth.TOKEN_PATH, endpoint=_post(TokenHandler(provider, _authenticator).handle),
          methods=_cors_post),
    Route(oauth.REGISTER_PATH, endpoint=_post(_register), methods=_cors_post),
    Route(oauth.REVOKE_PATH, endpoint=_post(RevocationHandler(provider, _authenticator).handle),
          methods=_cors_post),
])


# ── Consent ────────────────────────────────────────────────────────────────


def _consent_page(request: Request, pending, blob: str, *, error: str | None = None,
                  dial_code: str = "", national_number: str = "", status_code: int = 200):
    ctx = {
        "request": request,
        "logged_in": False,
        "active": "",
        "pending": pending,
        "request_blob": blob,
        "error": error,
        "countries": country_codes.options(),
        "selected_dial": (dial_code or "").strip().lstrip("+") or country_codes.DEFAULT_DIAL_CODE,
        "national_number": national_number,
    }
    return templates.TemplateResponse(request, "oauth_consent.html", ctx,
                                      status_code=status_code, headers=_CONSENT_HEADERS)


@router.get(oauth.CONSENT_PATH, response_class=HTMLResponse)
async def oauth_consent_page(request: Request):
    blob = request.query_params.get("request", "")
    pending = oauth.load_pending(blob)
    if pending is None:
        return _consent_page(request, None, "", status_code=400)
    return _consent_page(request, pending, blob)


@router.post(oauth.CONSENT_PATH)
async def oauth_consent_submit(
    request: Request,
    request_blob: str = Form("", alias="request"),
    decision: str = Form(""),
    country_code: str = Form(""),
    whatsapp_number: str = Form(""),
    app_password: str = Form(""),
):
    pending = oauth.load_pending(request_blob)
    if pending is None:
        return _consent_page(request, None, "", status_code=400)
    if decision != "allow":
        return RedirectResponse(oauth.deny(pending), status_code=303, headers=_NO_STORE)

    def again(message: str, status_code: int):
        return _consent_page(request, pending, request_blob, error=message, dial_code=country_code,
                             national_number=whatsapp_number, status_code=status_code)

    try:
        number = country_codes.normalize_whatsapp(country_code, whatsapp_number)
    except ValueError as e:
        return again(str(e), 400)

    ip = client_ip(request)
    try:
        login_throttle.check(number, ip)
    except login_throttle.TooManyAttempts as e:
        return again(str(e), 429)

    user = user_by_number(number)
    if not password_matches(user, app_password) or user is None or not user.is_active:
        # One message for every cause, as on /login.
        return again("That WhatsApp number and password don't match.", 401)
    login_throttle.clear(number, ip)

    try:
        target = oauth.approve(pending, user.id)
    except PermissionError:
        return again("That WhatsApp number and password don't match.", 401)
    return RedirectResponse(target, status_code=303, headers=_NO_STORE)


# ── Connected apps ─────────────────────────────────────────────────────────


@router.get("/api/oauth/connections")
async def api_oauth_connections(request: Request):
    user = current_user(request)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401, headers=_NO_STORE)
    return JSONResponse({"connections": oauth.list_grants(user.id)}, headers=_NO_STORE)


@router.delete("/api/oauth/connections/{grant_id}")
async def api_oauth_connection_revoke(request: Request, grant_id: int):
    user = current_user(request)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401, headers=_NO_STORE)
    if not oauth.revoke_grant_for_user(user.id, grant_id):
        return JSONResponse({"error": "Connection not found"}, status_code=404, headers=_NO_STORE)
    return JSONResponse({"ok": True}, headers=_NO_STORE)
