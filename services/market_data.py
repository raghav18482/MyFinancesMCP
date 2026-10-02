"""Angel One symbol resolution and candle fetching.

Several routers need the same symboltoken for the same tradingsymbol within a
page load. Resolving it once here, behind a short cache with a rate-limit
retry, is what keeps the app under Angel One's per-second request allowance.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

def scrip_search_key(tradingsymbol: str) -> str:
    """Root symbol for Angel searchScrip (e.g. GROWW from GROWW-BE, RELIANCE from RELIANCE-EQ)."""
    sym = (tradingsymbol or "").strip().upper()
    if "-" in sym:
        return sym.rsplit("-", 1)[0]
    return sym



def pick_scrip_row(data: list | None, requested_tradingsymbol: str) -> dict | None:
    """
    Pick the row whose tradingsymbol matches the chart/holding symbol.

    Angel returns multiple series (EQ, BE, BL, …); using data[0] often pairs the
    wrong symboltoken with the requested name and triggers AB4006 Invalid symboltoken.
    """
    if not data:
        return None
    req = (requested_tradingsymbol or "").strip()
    req_u = req.upper()
    for item in data:
        ts = item.get("tradingsymbol") or ""
        if ts == req or ts.upper() == req_u:
            return item
    base = scrip_search_key(req)
    if base and base.upper() != req_u:
        eq_sym = f"{base}-EQ"
        for item in data:
            ts = (item.get("tradingsymbol") or "").upper()
            if ts == eq_sym.upper():
                return item
    return data[0]



_search_scrip_cache: dict[tuple[str, str], tuple[float, dict]] = {}


_SCRIP_SEARCH_CACHE_TTL = 300.0  # seconds — cuts Angel rate limits across WS / candles / research



def search_scrip_cached(client, exchange: str, search_key: str) -> dict:
    """
    Cached ``searchScrip`` with one retry on rate-limit text from Angel.

    Many UI actions (candles + live WS + analytics) resolve the same symbol; caching
    avoids duplicate broker calls. Retry backs off briefly when Angel returns plain-text
    ``Access denied because of exceeding access rate`` (SmartApi raises DataException).
    """
    key = (exchange.upper(), search_key.upper())
    now = time.time()
    hit = _search_scrip_cache.get(key)
    if hit and (now - hit[0]) < _SCRIP_SEARCH_CACHE_TTL:
        return hit[1]
    for attempt in range(2):
        if attempt:
            time.sleep(2.5)
        try:
            out = client.search_scrip(exchange, search_key)
            _search_scrip_cache[key] = (time.time(), out)
            return out
        except Exception as e:
            low = str(e).lower()
            if attempt == 0 and ("exceeding access rate" in low or "access denied" in low):
                continue
            raise
    raise RuntimeError("search_scrip failed after retry")  # pragma: no cover



def fetch_candles_safe(client, exchange, token, interval, from_str, to_str):
    try:
        result = client.get_candle_data({
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_str,
            "todate": to_str,
        })
        if result and result.get("status") and result.get("data"):
            return result["data"]
    except Exception as e:
        logger.warning("Candle fetch failed for token %s: %s", token, e)
    return None
