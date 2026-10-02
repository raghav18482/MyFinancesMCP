"""LLM-backed portfolio insights and free-form questions."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


from services.ai_service import DEFAULT_OPENROUTER_MODEL, ask_question, generate_insights
from services.portfolio_service import build_portfolio_data


from web.dependencies import (
    require_login,
)

logger = logging.getLogger(__name__)

router = APIRouter()



@router.post("/api/ai/insights")
async def ai_insights(request: Request):
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    api_key = body.get("api_key", "")
    if not api_key:
        return JSONResponse(
            {"error": "Please enter your OpenRouter API key"}, status_code=400
        )

    model = body.get("model", DEFAULT_OPENROUTER_MODEL)
    portfolio = build_portfolio_data(client)

    try:
        insight = await generate_insights(api_key, portfolio, model)
        return JSONResponse({"insight": insight})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("AI insights error")
        return JSONResponse({"error": f"Something went wrong: {e}"}, status_code=500)



@router.post("/api/ai/ask")
async def ai_ask(request: Request):
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    api_key = body.get("api_key", "")
    if not api_key:
        return JSONResponse(
            {"error": "Please enter your OpenRouter API key"}, status_code=400
        )

    question = body.get("question", "")
    if not question:
        return JSONResponse({"error": "Question cannot be empty"}, status_code=400)

    model = body.get("model", DEFAULT_OPENROUTER_MODEL)
    portfolio = build_portfolio_data(client)

    try:
        answer = await ask_question(api_key, question, portfolio, model)
        return JSONResponse({"answer": answer})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("AI ask error")
        return JSONResponse({"error": f"Something went wrong: {e}"}, status_code=500)
