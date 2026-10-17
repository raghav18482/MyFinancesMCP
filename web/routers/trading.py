"""Risk profile, read-only proposal history and the user's own decision record.

There is no approval or execution endpoint: this application never places
orders. Proposals listed here are history only.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


from services import compliance, decision_record
from services.risk_profile import risk_profiles, build_profile_from_dict, load_profile, save_profile
from services.trade_proposals import proposal_store

from web.dependencies import (
    current_client,
    current_user,
    session_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()



# ── Trading API ────────────────────────────────────────────────────────────


@router.get("/api/trading/profile")
async def api_trading_profile_get(request: Request):
    client = current_client(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    # Read after current_client, which may have rebuilt the session under a new sid.
    sid = session_id(request)
    profile = risk_profiles.get(sid)
    if profile is None:
        user = current_user(request)
        if user:
            profile = load_profile(user.id)
            if profile:
                risk_profiles.set(sid, profile)
    if profile is None:
        return JSONResponse({"has_profile": False})
    return JSONResponse({"has_profile": True, "profile": profile.to_dict()})



@router.post("/api/trading/profile")
async def api_trading_profile_set(request: Request):
    client = current_client(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    # Read after current_client, which may have rebuilt the session under a new sid.
    sid = session_id(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    try:
        profile = build_profile_from_dict(body)
        risk_profiles.set(sid, profile)
        user = current_user(request)
        if user:
            save_profile(user.id, profile)
        return JSONResponse({"ok": True, "profile": profile.to_dict()})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)



@router.get("/api/trading/proposals")
async def api_trading_proposals(request: Request):
    if current_client(request) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sid = session_id(request)
    proposals = proposal_store.list_for_session(sid)
    return JSONResponse({
        "proposals": [p.to_dict() for p in proposals],
        "count": len(proposals),
    })




@router.get("/api/trading/compliance")
async def api_trading_compliance(request: Request):
    """Which mandate ruleset is in force, and whether it is still the template.

    Surfaced so the UI can warn that the shipped limits are placeholders rather
    than letting them look authoritative.
    """
    if current_client(request) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        return JSONResponse(compliance.ruleset_info())
    except Exception as e:
        logger.exception("Compliance ruleset error")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/trading/decisions")
async def api_trading_decisions(request: Request):
    """The signed-in user's own decision records, newest first.

    Only rows owned by this user are returned. Records with unknown ownership
    are never shown to anyone here; there is no admin audit view.
    """
    if current_client(request) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = current_user(request)
    if user is None:
        return JSONResponse({"decisions": []})

    symbol = request.query_params.get("symbol")
    try:
        limit = max(1, min(int(request.query_params.get("limit", 50)), 200))
    except ValueError:
        limit = 50

    try:
        return JSONResponse({"decisions": decision_record.recent(limit, user_id=user.id, symbol=symbol)})
    except Exception:
        logger.exception("Decision record read error")
        return JSONResponse({"error": "Could not read decisions"}, status_code=500)


@router.post("/api/trading/proposals/{proposal_id}/reject")
async def api_trading_reject(request: Request, proposal_id: str):
    if current_client(request) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sid = session_id(request)
    try:
        proposal = proposal_store.reject(sid, proposal_id)
        return JSONResponse({"ok": True, "status": proposal.effective_status})
    except (ValueError, PermissionError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
