"""
Phase 1 daily portfolio briefing for a single Angel One user (production-safe).

For one ``session_id``:
1. Fetch holdings from Angel One.
2. Per stock: 70 days of daily candles (using the symboltoken already in the
   holdings response, so no extra ``search_scrip`` call) -> drop_3d / 1w / 1m
   + LightGBM ``predict_direction`` predictions for 1day / 1week.
3. Send the structured snapshot to OpenRouter with a fixed prompt.
4. Return a WhatsApp-ready plain-text string with per-stock signals
   (green / yellow / red) decided by the LLM.

Throttling and graceful degradation:
- Calls to Angel are throttled (``_INTER_SYMBOL_DELAY_SEC``) to stay under
  Angel's 3 req/sec historical-candle limit.
- Rate-limit responses ("Access denied because of exceeding access rate") are
  retried once after ``_RATE_LIMIT_RETRY_DELAY_SEC``; only then the symbol is
  dropped into the ``skipped`` list.
- One bad symbol never fails the whole briefing.

No scheduler, no DB, no WhatsApp send -- those come in later phases.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from openai import APIError, AuthenticationError

from services.ai_service import DEFAULT_OPENROUTER_MODEL, _make_client
from services.prediction_service import predict_direction
from session_manager import sessions

logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────
_HISTORY_DAYS = 70
_INTER_SYMBOL_DELAY_SEC = 0.3
_RATE_LIMIT_RETRY_DELAY_SEC = 1.5
_MIN_CANDLES_REQUIRED = 5
_RATE_LIMIT_TOKENS = (
    "exceeding access rate",
    "access denied",
    "rate limit",
    "too many requests",
)


SYSTEM_PROMPT = """You are a WhatsApp portfolio briefer for an Indian retail
investor on NSE. You receive a JSON snapshot of the user's holdings with, for
each stock: current price, % change vs 3 days / 1 week / 1 month ago, and
LightGBM model predictions for 1-day and 1-week direction.

Decide a signal per stock and emit a WhatsApp-ready message:
- 🟢  Good buy zone today: price has meaningfully dropped recently (typically
       at least one of 3d/1w/1m drop ≤ -3%) AND ML 1-day/1-week is not bearish.
- 🔴  Don't buy today: price has run up sharply (e.g. 3d > +3% or 1w > +5%) OR
       ML 1-day/1-week is bearish with reasonable confidence.
- 🟡  Neutral: anything in between.

Output rules:
- One short opening line: "Daily portfolio brief — <date>".
- One line per stock: "<emoji> SYMBOL ₹LTP — <one-line reason>".
- For 🟢 and 🟡 stocks, include the drop figure(s) from 3d / 1w / 1m that
  matter (e.g. "down 4.2% over 1w, 7.1% over 1m").
- For 🔴 stocks, give the reason in ≤8 words ("up 6% in 3d", "ML bearish 1w").
- Keep total under ~1500 characters (WhatsApp friendly).
- If the input's `skipped` list is non-empty, add one extra line immediately
  before the disclaimer: "Skipped: SYM1, SYM2, ..." (comma-separated symbol
  names from `skipped[*].symbol`, no extra commentary).
- End with: "Not financial advice."
- No markdown, no code blocks, plain text only.
"""


def _is_rate_limit_error(err: Any) -> bool:
    s = str(err).lower()
    return any(tok in s for tok in _RATE_LIMIT_TOKENS)


def _pct_change(closes: list[float], n: int) -> float | None:
    """Percent change from n bars ago to the latest close. None if insufficient data."""
    if len(closes) < n + 1 or closes[-n] in (0, 0.0):
        return None
    return round((closes[-1] - closes[-n]) / closes[-n] * 100, 2)


def _holdings_records(client: Any) -> list[dict[str, Any]]:
    """Read holdings directly so we keep symboltoken + exchange and skip search_scrip."""
    raw = client.get_holdings()
    if not raw.get("status") or not raw.get("data"):
        return []
    out: list[dict[str, Any]] = []
    for h in raw["data"]:
        qty = int(h.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        token = str(h.get("symboltoken") or "").strip()
        if not token:
            continue
        out.append(
            {
                "symbol": h.get("tradingsymbol") or "N/A",
                "exchange": (h.get("exchange") or "NSE").strip() or "NSE",
                "token": token,
                "qty": qty,
                "ltp_holding": float(h.get("ltp") or 0),
            }
        )
    return out


def _fetch_candles_by_token(
    client: Any,
    exchange: str,
    token: str,
    days: int = _HISTORY_DAYS,
    interval: str = "ONE_DAY",
) -> list[list[Any]]:
    """Single Angel API call (no search_scrip). Returns the candle list or raises."""
    todate = datetime.now()
    fromdate = todate - timedelta(days=days)
    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": interval,
        "fromdate": fromdate.strftime("%Y-%m-%d %H:%M"),
        "todate": todate.strftime("%Y-%m-%d %H:%M"),
    }
    result = client.get_candle_data(params)
    if not result.get("status") or not result.get("data"):
        raise RuntimeError(result.get("message") or "candle fetch failed")
    return result["data"]


def _collect_stock_snapshot(client: Any, rec: dict[str, Any]) -> dict[str, Any]:
    """Build one stock's snapshot. Raises on unrecoverable errors."""
    candles = _fetch_candles_by_token(client, rec["exchange"], rec["token"])
    if len(candles) < _MIN_CANDLES_REQUIRED:
        raise RuntimeError(f"only {len(candles)} candles, need at least {_MIN_CANDLES_REQUIRED}")

    closes = [float(c[4]) for c in candles]
    last = closes[-1]

    drops = {
        "3d": _pct_change(closes, 3),
        "1w": _pct_change(closes, 5),
        "1m": _pct_change(closes, 22),
    }

    ml_block: dict[str, Any] = {}
    try:
        ml = predict_direction(candles, rec["symbol"])
        if "error" in ml:
            ml_block = {"error": ml["error"]}
        else:
            preds = ml.get("predictions", {})
            ml_block = {
                "outlook": ml.get("overall_outlook"),
                "1day": preds.get("1day"),
                "1week": preds.get("1week"),
            }
    except Exception as e:
        logger.warning("predict_direction failed for %s: %s", rec["symbol"], e)
        ml_block = {"error": str(e)}

    return {
        "symbol": rec["symbol"],
        "ltp": round(last, 2),
        "drop_pct": drops,
        "ml": ml_block,
    }


async def _snapshot_with_retry(
    client: Any, rec: dict[str, Any], counters: dict[str, int]
) -> dict[str, Any]:
    """Call ``_collect_stock_snapshot`` with one retry on Angel rate-limit responses."""
    try:
        return await asyncio.to_thread(_collect_stock_snapshot, client, rec)
    except Exception as e:
        if not _is_rate_limit_error(e):
            raise
        counters["rate_limited"] = counters.get("rate_limited", 0) + 1
        logger.warning(
            "rate-limited on %s, retrying in %.1fs", rec["symbol"], _RATE_LIMIT_RETRY_DELAY_SEC
        )
        await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY_SEC)
        return await asyncio.to_thread(_collect_stock_snapshot, client, rec)


async def generate_daily_briefing(
    session_id: str,
    *,
    openrouter_api_key: str | None = None,
    model: str = DEFAULT_OPENROUTER_MODEL,
) -> str:
    """Build a WhatsApp-ready daily portfolio brief for one Angel One user.

    Caller must have already created an Angel session via
    ``session_manager.sessions.create_session(session_id, ...)``.
    """
    client = sessions.get_client(session_id)
    if client is None:
        raise RuntimeError(
            f"No Angel One session for id '{session_id}'. "
            "Call sessions.create_session(...) first."
        )

    records = _holdings_records(client)
    if not records:
        logger.info("briefing: no holdings for session %s", session_id[:8])
        return "Daily portfolio brief — no holdings found. Not financial advice."

    logger.info("briefing: processing %d holdings for session %s", len(records), session_id[:8])

    counters: dict[str, int] = {"rate_limited": 0}
    snapshots: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for idx, rec in enumerate(records):
        sym = rec["symbol"]
        try:
            snap = await _snapshot_with_retry(client, rec, counters)
            snapshots.append(snap)
        except Exception as e:
            if _is_rate_limit_error(e):
                counters["rate_limited"] = counters.get("rate_limited", 0) + 1
            logger.warning("snapshot failed for %s: %s", sym, e)
            skipped.append({"symbol": sym, "error": str(e)})

        if idx < len(records) - 1:
            await asyncio.sleep(_INTER_SYMBOL_DELAY_SEC)

    logger.info(
        "briefing: complete | ok=%d skipped=%d rate_limited=%d total=%d",
        len(snapshots),
        len(skipped),
        counters["rate_limited"],
        len(records),
    )

    payload = {
        "as_of": datetime.now().isoformat(timespec="minutes"),
        "holdings": snapshots,
        "skipped": skipped,
    }

    api_key = (openrouter_api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is required to generate the briefing.")

    oai = _make_client(api_key)
    try:
        resp = await oai.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, default=str)},
            ],
            temperature=0.3,
            max_tokens=1200,
        )
    except AuthenticationError:
        raise ValueError("Invalid OpenRouter API key.") from None
    except APIError as e:
        raise ValueError(f"OpenRouter API error: {getattr(e, 'message', None) or e}") from None

    return (resp.choices[0].message.content or "").strip()


def _bootstrap_angel_session_from_env(session_id: str) -> None:
    """Mirror agents/finance/agent.py: create an Angel session from .env if missing."""
    if sessions.get_client(session_id) is not None:
        return
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    api_key = (os.environ.get("ANGELONE_API_KEY") or os.environ.get("ANGEL_API_KEY") or "").strip()
    client_id = (os.environ.get("ANGELONE_CLIENT_ID") or os.environ.get("ANGEL_CLIENT_ID") or "").strip()
    password = (os.environ.get("ANGELONE_PASSWORD") or os.environ.get("ANGEL_PASSWORD") or "").strip()
    totp = (os.environ.get("ANGELONE_TOTP_SECRET") or os.environ.get("ANGEL_TOTP_SECRET") or "").strip()
    if not (api_key and client_id and password and totp):
        raise RuntimeError(
            "Missing Angel One credentials in environment "
            "(ANGELONE_API_KEY / ANGELONE_CLIENT_ID / ANGELONE_PASSWORD / ANGELONE_TOTP_SECRET)."
        )
    sessions.create_session(session_id, api_key, client_id, password, totp)


if __name__ == "__main__":
    import sys as _sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")

    # Optional: pass a phone number as the first CLI argument to also send via WhatsApp.
    # Usage:  python -m services.schedular.daily_briefing [phone]
    # e.g.:   python -m services.schedular.daily_briefing 8107037133
    _phone: str | None = _sys.argv[1] if len(_sys.argv) > 1 else None

    async def _run() -> None:
        sid = "daily-briefing-default"
        _bootstrap_angel_session_from_env(sid)
        message = await generate_daily_briefing(sid)
        print("\n" + "=" * 60)
        print(message)
        print("=" * 60)
        if _phone:
            from services.schedular.whatsapp import send as _wa_send
            print(f"\nSending to {_phone} via WhatsApp...")
            await _wa_send(_phone, message)
            print("WhatsApp message sent.")

    asyncio.run(_run())
