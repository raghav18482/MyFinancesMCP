"""Public feedback form endpoint."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


from services import feedback as feedback_service
from services.schedular import whatsapp


from web.dependencies import (
    client_ip,
)

logger = logging.getLogger(__name__)

router = APIRouter()



@router.get("/api/feedback/token")
async def api_feedback_token():
    """Mint the single-use token ``POST /api/feedback`` requires.

    The server-rendered landing page embeds one at render time. The React app
    fetches one immediately before it submits instead, so a form left open past
    the token's ten-minute lifetime still works. Minting needs no limit of its
    own: anyone can already mint by loading ``/``, and the per-IP cap on
    submissions is what bounds how many tokens are worth having.
    """
    try:
        token = feedback_service.issue_feedback_token()
    except Exception:
        # ENCRYPTION_KEY missing or misconfigured.
        logger.exception("Could not issue feedback token")
        return JSONResponse(
            {"error": "Feedback is unavailable right now."}, status_code=503
        )
    return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})



@router.post("/api/feedback")
async def api_feedback(request: Request):
    """Public landing-page feedback → AI reply → WhatsApp.

    Guarded by an ENCRYPTION_KEY-signed, single-use, short-lived token issued at
    landing render time, plus per-IP rate limiting.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # Honeypot: real users never fill this hidden field.
    if (body.get("website") or "").strip():
        return JSONResponse({"ok": True, "message": "Thanks for your feedback!"})

    token = (body.get("token") or "").strip()
    phone = (body.get("phone") or "").strip()
    feedback = (body.get("feedback") or "").strip()
    name = (body.get("name") or "").strip() or None

    try:
        feedback_service.consume_feedback_token(token)
        feedback_service.check_rate_limit(client_ip(request))
    except feedback_service.FeedbackError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    if not feedback:
        return JSONResponse({"error": "Please enter your feedback."}, status_code=400)
    if len(feedback) > feedback_service._MAX_FEEDBACK_CHARS:
        return JSONResponse({"error": "Feedback is too long."}, status_code=400)

    try:
        normalised_phone = whatsapp._normalise_phone(phone)
    except ValueError:
        return JSONResponse(
            {"error": "Please enter a valid WhatsApp number."}, status_code=400
        )

    try:
        reply = await feedback_service.generate_reply(feedback, name)
        await whatsapp.send(normalised_phone, reply)
    except Exception:
        logger.exception("Feedback WhatsApp send failed")
        return JSONResponse(
            {"error": "Could not send the message right now. Please try again later."},
            status_code=502,
        )

    return JSONResponse({"ok": True, "message": "Sent! Check your WhatsApp."})
