"""Live price streaming over Server-Sent Events."""
from __future__ import annotations

import json
import asyncio
import logging

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse


from services.realtime_feed import Subscription, stream_ticks
from services.market_data import (
    pick_scrip_row,
    scrip_search_key,
    search_scrip_cached,
)


from web.dependencies import (
    require_login,
    session_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()



# ── Real-time market stream (SSE) ──────────────────────────────────────────

# Hard cap so one browser cannot fan out an unbounded number of subscriptions.
_MAX_STREAM_SYMBOLS = 12


# Comment frame cadence; keeps proxies and load balancers from idling us out.
_SSE_HEARTBEAT = 15.0



def _sse(event: str, payload: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"



async def _resolve_stream_token(
    client, exchange: str, symbol: str, hint: str | None = None
) -> tuple[str, str] | None:
    """Resolve (symboltoken, tradingsymbol); returns None when unresolvable."""
    if hint and hint.strip().isdigit():
        return hint.strip(), symbol

    scrip = await asyncio.to_thread(
        search_scrip_cached, client, exchange, scrip_search_key(symbol)
    )
    if not isinstance(scrip, dict) or not scrip.get("status") or not scrip.get("data"):
        return None
    match = pick_scrip_row(scrip["data"], symbol)
    if not match or not match.get("symboltoken"):
        return None
    return str(match["symboltoken"]), match.get("tradingsymbol") or symbol



@router.get("/api/market/stream")
async def api_market_stream(
    request: Request,
    symbols: str = Query(..., description="Comma-separated trading symbols"),
    tokens: str = Query("", description="Optional comma-separated symboltokens"),
    exchange: str = Query("NSE"),
):
    """
    Stream live price ticks to the browser over Server-Sent Events.

    One stream carries every requested symbol, so a page watching several
    stocks holds a single HTTP connection rather than one per symbol.
    Authentication uses the session cookie, so no session id travels in the
    URL. Ticks come from the Angel WebSocket feed where available and from
    REST polling only for tokens the feed is not delivering.
    """
    client = require_login(request)
    if client is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    sid = session_id(request)
    requested = [s.strip() for s in symbols.split(",") if s.strip()][:_MAX_STREAM_SYMBOLS]
    if not requested:
        return JSONResponse({"error": "No symbols requested"}, status_code=400)

    hints = [t.strip() for t in tokens.split(",")] if tokens else []

    subs: list[Subscription] = []
    unresolved: list[str] = []
    try:
        for i, sym in enumerate(requested):
            hint = hints[i] if i < len(hints) else None
            resolved = await _resolve_stream_token(client, exchange, sym, hint)
            if resolved is None:
                unresolved.append(sym)
                continue
            token, tradingsymbol = resolved
            subs.append(
                Subscription(symboltoken=token, tradingsymbol=tradingsymbol, exchange=exchange)
            )
    except Exception as e:
        msg = str(e).lower()
        logger.warning("Symbol lookup failed for market stream: %s", e)
        if "exceeding access rate" in msg or "access denied" in msg:
            detail = (
                "Angel One rate limit: too many API calls in a short window. "
                "Wait 30-60 seconds, then reload the page."
            )
        else:
            detail = "Symbol lookup failed (broker error). Try again in a moment."
        return JSONResponse({"error": detail}, status_code=503)

    if not subs:
        return JSONResponse(
            {"error": f"Could not resolve any of: {', '.join(requested)}"},
            status_code=404,
        )

    async def event_source():
        stream = stream_ticks(client, sid, subs)
        try:
            yield _sse(
                "subscribed",
                {
                    "symbols": [
                        {"tradingsymbol": s.tradingsymbol, "symboltoken": s.symboltoken}
                        for s in subs
                    ],
                    "unresolved": unresolved,
                },
            )
            while True:
                try:
                    tick = await asyncio.wait_for(
                        stream.__anext__(), timeout=_SSE_HEARTBEAT
                    )
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                except StopAsyncIteration:
                    break
                yield _sse("tick", tick.to_dict())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Market stream ended for %s: %s", sid[:8], e)
        finally:
            await stream.aclose()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Tell nginx not to buffer; without this SSE appears to hang.
            "X-Accel-Buffering": "no",
        },
    )
