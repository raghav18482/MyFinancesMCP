"""OAuth connection and account selection for supported broker accounts."""
from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from kiteconnect import KiteConnect

from adminApi import broker_accounts as broker_account_service
from adminApi import service as admin_service
from integrations.zerodha_client import ZerodhaClient
from services.realtime_feed import feed_relay
from session_manager import sessions
from web.dependencies import current_app_user, current_client, session_id, start_session

logger = logging.getLogger(__name__)
router = APIRouter()
_STATE_KEY = "zerodha_oauth_state"
_STARTED_KEY = "zerodha_oauth_started"
_NO_STORE = {"Cache-Control": "no-store"}


def _broker_redirect(request: Request, status: str) -> RedirectResponse:
    """Send status to a usable page, including before the first broker link."""
    destination = "/dashboard" if current_client(request) is not None else "/accounts"
    return RedirectResponse(f"{destination}?broker={status}", status_code=302)


def _kite_config() -> tuple[str, str, str]:
    api_key = (os.environ.get("KITE_API_KEY") or "").strip()
    api_secret = (os.environ.get("KITE_API_SECRET") or "").strip()
    redirect_url = (os.environ.get("KITE_REDIRECT_URL") or "").strip()
    if not (api_key and api_secret and redirect_url):
        raise RuntimeError("Zerodha connection is not configured on this server")
    parsed = urlsplit(redirect_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path != "/auth/zerodha/callback":
        raise RuntimeError("KITE_REDIRECT_URL must point to /auth/zerodha/callback")
    return api_key, api_secret, redirect_url


@router.get("/auth/zerodha/connect")
async def connect_zerodha(request: Request):
    """Start Kite's hosted login for the authenticated MyFinance account."""
    if current_app_user(request) is None:
        return RedirectResponse("/login?connect=zerodha", status_code=302)
    try:
        api_key, _api_secret, _redirect_url = _kite_config()
        kite = KiteConnect(api_key=api_key)
        state = uuid.uuid4().hex
        request.session[_STATE_KEY] = state
        request.session[_STARTED_KEY] = int(time.time())
        return RedirectResponse(
            f"{kite.login_url()}&redirect_params={quote('state=' + state)}",
            status_code=302,
        )
    except Exception:
        logger.exception("Unable to start Zerodha OAuth")
        return _broker_redirect(request, "zerodha-unavailable")


@router.get("/auth/zerodha/callback", name="zerodha_callback")
async def zerodha_callback(request: Request):
    """Exchange the one-time request token and attach the verified account."""
    uid = request.session.get("uid")
    expected_state = request.session.pop(_STATE_KEY, None)
    started = request.session.pop(_STARTED_KEY, 0)
    state = request.query_params.get("state", "")
    if not uid or not expected_state or not hmac.compare_digest(str(expected_state), state):
        return RedirectResponse("/login?connect=zerodha-invalid", status_code=302)
    if not isinstance(started, int) or time.time() - started > 600:
        return _broker_redirect(request, "zerodha-expired")
    if request.query_params.get("status") != "success":
        return _broker_redirect(request, "zerodha-cancelled")

    request_token = request.query_params.get("request_token", "")
    if not request_token or len(request_token) > 256:
        return _broker_redirect(request, "zerodha-invalid")

    try:
        api_key, api_secret, _redirect_url = _kite_config()
        if request.url.path != urlsplit(_redirect_url).path:
            return RedirectResponse("/login?connect=zerodha-invalid", status_code=302)
        kite = KiteConnect(api_key=api_key)
        session_data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session_data.get("access_token")
        profile = kite.profile()
        if not access_token or not profile.get("user_id"):
            raise ValueError("Zerodha did not return a valid account session")
        account_id = broker_account_service.save_zerodha_account(int(uid), profile, access_token)
        client = ZerodhaClient(api_key, access_token, profile)
        client.app_user_id = int(uid)
        client.ensure_session()
        old_sid = session_id(request)
        if old_sid:
            feed_relay.stop_feed(old_sid)
            sessions.remove_session(old_sid)
        sid = uuid.uuid4().hex
        sessions.create_client_session(sid, client)
        request.session["sid"] = sid
        request.session["broker_account_id"] = account_id
        return _broker_redirect(request, "zerodha-connected")
    except Exception:
        logger.exception("Zerodha account connection failed for app user id=%s", uid)
        return _broker_redirect(request, "zerodha-error")


@router.get("/api/accounts")
async def get_linked_accounts(request: Request):
    uid = request.session.get("uid")
    if not uid or current_app_user(request) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401, headers=_NO_STORE)
    try:
        return JSONResponse({"accounts": broker_account_service.list_accounts(int(uid)),
                             "active_account_id": request.session.get("broker_account_id")},
                            headers=_NO_STORE)
    except Exception as exc:
        logger.exception("Linked broker account lookup failed")
        return JSONResponse({"error": str(exc)}, status_code=500, headers=_NO_STORE)


@router.post("/api/accounts/{account_id}/activate")
async def activate_linked_account(request: Request, account_id: int):
    uid = request.session.get("uid")
    if not uid or current_client(request) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401, headers=_NO_STORE)
    try:
        owned = {int(a["id"]) for a in broker_account_service.list_accounts(int(uid))}
        if account_id not in owned:
            return JSONResponse({"error": "Account not found"}, status_code=404, headers=_NO_STORE)
        old_sid = session_id(request)
        if old_sid:
            feed_relay.stop_feed(old_sid)
            sessions.remove_session(old_sid)
        request.session.pop("sid", None)
        request.session["broker_account_id"] = account_id
        client = current_client(request)
        if client is None:
            return JSONResponse({"error": "Zerodha session expired. Connect the account again."},
                                status_code=401, headers=_NO_STORE)
        return JSONResponse({"ok": True, "client_id": client.client_id,
                             "broker": client.broker}, headers=_NO_STORE)
    except Exception as exc:
        logger.exception("Could not activate linked account id=%s", account_id)
        return JSONResponse({"error": str(exc)}, status_code=500, headers=_NO_STORE)


@router.post("/api/accounts/activate/angel")
async def activate_angel_account(request: Request):
    """Switch to a previously configured Angel One account without losing the active broker."""
    uid = request.session.get("uid")
    if not uid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401, headers=_NO_STORE)
    creds = admin_service.credentials_for_user(int(uid))
    if creds is None:
        return JSONResponse({"error": "No Angel One account is linked to this app account."},
                            status_code=409, headers=_NO_STORE)
    old_sid = session_id(request)
    try:
        new_sid, client = start_session(request, creds)
    except Exception:
        logger.exception("Could not activate Angel One account for app user id=%s", uid)
        return JSONResponse({"error": "Could not connect the Angel One account. Please reconnect it."},
                            status_code=401, headers=_NO_STORE)
    request.session.pop("broker_account_id", None)
    if old_sid and old_sid != new_sid:
        feed_relay.stop_feed(old_sid)
        sessions.remove_session(old_sid)
    return JSONResponse({"ok": True, "client_id": client.client_id,
                         "broker": getattr(client, "broker", "angelone")}, headers=_NO_STORE)
