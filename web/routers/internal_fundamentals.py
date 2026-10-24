"""Endpoints for the scheduled fundamentals job, not for browsers or users.

``scripts/refresh_fundamentals.py`` runs on GitHub Actions, where Yahoo answers,
and calls these to learn which stocks to fetch and to hand the results over.
Authenticated by GitHub's signed token for that workflow
(:mod:`services.github_oidc`), never by a user session.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services import fundamental_service, fundamental_store, github_oidc

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_ITEMS = 100  # per post; the job sends batches of 25
_MAX_BODY_BYTES = 5 * 1024 * 1024


def _refuse(request: Request) -> Optional[JSONResponse]:
    """None if the caller is our workflow, else the 401 to send."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        github_oidc.verify(auth[7:].strip())
    except github_oidc.NotOurWorkflow as e:
        logger.warning("fundamentals job request refused: %s", e)
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return None


@router.get("/api/internal/fundamentals/queue")
async def fundamentals_queue(request: Request):
    """Stocks users are waiting for, and when each stored stock was fetched."""
    # Verifying may fetch GitHub's keys, and the store is a blocking DB call.
    refused = await asyncio.to_thread(_refuse, request)
    if refused:
        return refused
    try:
        return JSONResponse(await asyncio.to_thread(fundamental_store.queue))
    except Exception:
        logger.exception("fundamentals queue failed")
        return JSONResponse({"error": "Store unavailable"}, status_code=503)


@router.post("/api/internal/fundamentals")
async def fundamentals_ingest(request: Request):
    """Store fetched fundamentals; drop requests for stocks Yahoo has no data for."""
    refused = await asyncio.to_thread(_refuse, request)
    if refused:
        return refused
    if int(request.headers.get("content-length") or 0) > _MAX_BODY_BYTES:
        return JSONResponse({"error": "Body too large"}, status_code=413)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    items = body.get("items") if isinstance(body, dict) else None
    no_data = body.get("no_data", []) if isinstance(body, dict) else None
    if not isinstance(items, list) or not isinstance(no_data, list):
        return JSONResponse({"error": "Expected {items: [...], no_data: [...]}"}, status_code=400)
    if len(items) + len(no_data) > _MAX_ITEMS:
        return JSONResponse({"error": f"At most {_MAX_ITEMS} entries per post"}, status_code=400)

    def store() -> dict:
        saved = {
            fundamental_store.key(item.get("symbol"))
            for item in items
            if isinstance(item, dict) and fundamental_store.save(item)
        }
        fundamental_service.drop_cached(saved)
        dropped = fundamental_store.forget_requests(s for s in no_data if isinstance(s, str))
        return {"saved": len(saved), "dropped_requests": dropped}

    return JSONResponse(await asyncio.to_thread(store))
