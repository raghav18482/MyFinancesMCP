"""Risk profile and trade proposal approval endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


from services.risk_profile import risk_profiles, build_profile_from_dict, load_profile, save_profile
from services.trade_proposals import proposal_store, execute_proposal

from session_manager import sessions

from web.dependencies import (
    registered_user_for_session,
    session_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()



# ── Trading API ────────────────────────────────────────────────────────────


@router.get("/api/trading/profile")
async def api_trading_profile_get(request: Request):
    sid = session_id(request)
    client = sessions.get_client(sid) if sid else None
    if not sid or client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    profile = risk_profiles.get(sid)
    if profile is None:
        user = registered_user_for_session(client)
        if user:
            profile = load_profile(user.id)
            if profile:
                risk_profiles.set(sid, profile)
    if profile is None:
        return JSONResponse({"has_profile": False})
    return JSONResponse({"has_profile": True, "profile": profile.to_dict()})



@router.post("/api/trading/profile")
async def api_trading_profile_set(request: Request):
    sid = session_id(request)
    client = sessions.get_client(sid) if sid else None
    if not sid or client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    try:
        profile = build_profile_from_dict(body)
        risk_profiles.set(sid, profile)
        user = registered_user_for_session(client)
        if user:
            save_profile(user.id, profile)
        return JSONResponse({"ok": True, "profile": profile.to_dict()})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)



@router.get("/api/trading/proposals")
async def api_trading_proposals(request: Request):
    sid = session_id(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    proposals = proposal_store.list_for_session(sid)
    return JSONResponse({
        "proposals": [p.to_dict() for p in proposals],
        "count": len(proposals),
    })



@router.post("/api/trading/proposals/{proposal_id}/approve")
async def api_trading_approve(request: Request, proposal_id: str):
    sid = session_id(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    client = sessions.get_client(sid)
    try:
        proposal_store.approve(sid, proposal_id)
        result = execute_proposal(sid, proposal_id, client)
        return JSONResponse(result)
    except (ValueError, PermissionError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("Proposal approval error")
        return JSONResponse({"error": str(e)}, status_code=500)



@router.post("/api/trading/proposals/{proposal_id}/reject")
async def api_trading_reject(request: Request, proposal_id: str):
    sid = session_id(request)
    if not sid or sessions.get_client(sid) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        proposal = proposal_store.reject(sid, proposal_id)
        return JSONResponse({"ok": True, "status": proposal.effective_status})
    except (ValueError, PermissionError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
