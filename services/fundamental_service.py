import time
import logging
from datetime import datetime, timezone
from typing import Optional

import yfinance as yf
from yfinance.data import YfData

from services import fundamental_store, yahoo_gateway
from services.fundamental_scoring import score_fundamentals
from services.yahoo_gateway import YahooNotFound, YahooUnavailable

logger = logging.getLogger(__name__)

_cache: dict[str, dict] = {}
_CACHE_TTL = 6 * 3600  # 6 hours
# A stale answer is held only briefly in memory, so the next request soon
# tries Yahoo again rather than serving old figures for six hours.
_STALE_CACHE_TTL = 10 * 60

_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
_INFO_ATTEMPTS = 2
_RETRY_BACKOFF = 1.5  # seconds, multiplied by the attempt number
# Only these are worth retrying at once. Retrying a 429 or 403 extends the ban.
_RETRYABLE_NOW = {yahoo_gateway.TIMEOUT, yahoo_gateway.NETWORK, yahoo_gateway.UPSTREAM}
_STATEMENTS = ("financials", "balance_sheet", "major_holders")


class YahooNoData(Exception):
    """Yahoo answered and has nothing for this symbol (unlisted on Yahoo)."""


def _cache_get(key: str) -> Optional[dict]:
    entry = _cache.get(key)
    if entry and time.time() < entry["expires"]:
        return entry["data"]
    return None


def _cache_set(key: str, data: dict, ttl: float = _CACHE_TTL):
    _cache[key] = {"data": data, "expires": time.time() + ttl}


def _yf_symbol(trading_symbol: str) -> str:
    base = trading_symbol.replace("-EQ", "").replace("-BE", "")
    return f"{base}.NS"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_stock_fundamentals(trading_symbol: str, *, use_store: bool = True) -> dict:
    """Valuation, health, trends and the 0-100 score for one stock.

    Sources, in order:

    1. this process's memory (6h);
    2. the Postgres snapshot, when younger than a day: no Yahoo call at all;
    3. Yahoo, through :mod:`services.yahoo_gateway`;
    4. when Yahoo fails, the Postgres snapshot however old (up to 45 days),
       marked ``stale`` with its ``as_of`` date.

    Every result carries ``as_of`` and ``source`` (``live``, ``store`` or
    ``stale``). A Yahoo failure with nothing stored comes back with
    ``retryable``, ``cause`` and ``retry_after``; the API turns that into a 503.
    "Yahoo has no such symbol" (ETFs) stays a plain ``error``.

    ``use_store=False`` skips step 2, for the refresh script.
    """
    cache_key = f"fundamental:{trading_symbol}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    snapshot = fundamental_store.load(trading_symbol)
    if use_store and snapshot:
        stored, fetched_at = snapshot
        if fundamental_store.age_seconds(fetched_at) < fundamental_store.FRESH_FOR_SEC:
            result = dict(stored, as_of=_iso(fetched_at), source="store", stale=False)
            _cache_set(cache_key, result)
            return result

    yf_sym = _yf_symbol(trading_symbol)
    result: dict = {
        "symbol": trading_symbol,
        "yf_symbol": yf_sym,
        "valuation": {},
        "health": {},
        "revenue_trend": [],
        "profit_trend": [],
        "score": None,
        "error": None,
    }

    try:
        _fetch_live(yf_sym, result)
    except YahooNoData:
        # Not cached: a symbol Yahoo adds later should show up without a wait.
        result["error"] = f"No data found for {yf_sym}"
        result["score"] = score_fundamentals(result)
        return result
    except YahooUnavailable as e:
        logger.warning(
            "Fundamental fetch failed for %s (%s): %s: %s",
            trading_symbol, yf_sym, e.cause, e.detail,
        )
        if snapshot and fundamental_store.age_seconds(snapshot[1]) < fundamental_store.STALE_FOR_SEC:
            stored, fetched_at = snapshot
            stale = dict(stored, as_of=_iso(fetched_at), source="stale", stale=True,
                         stale_reason=e.cause)
            _cache_set(cache_key, stale, ttl=_STALE_CACHE_TTL)
            return stale
        result["error"] = f"Couldn't reach Yahoo Finance for {yf_sym}. Please try again shortly."
        result["retryable"] = True
        result["cause"] = e.cause
        result["retry_after"] = e.retry_after
        result["score"] = score_fundamentals(result)
        return result

    yahoo_gateway.record_success()
    now = datetime.now(timezone.utc)
    result.update(as_of=_iso(now), source="live", stale=False)
    # Scored last so the 0-100 breakdown rides the same caches, and so the
    # agent tool and the research API both get it without extra work.
    result["score"] = score_fundamentals(result)

    _cache_set(cache_key, result)
    fundamental_store.save(trading_symbol, result)

    # Every live fetch becomes a point-in-time snapshot. yfinance only ever
    # reports the current, possibly restated figure, so the only way to know
    # what was knowable on a past date is to have written it down on that date.
    # Best-effort: a snapshot failure must never break the caller's request.
    try:
        from services.marketstore import pit

        pit.write_snapshot(result)
    except Exception as e:  # pragma: no cover - defensive, see pit.write_snapshot
        logger.debug("point-in-time snapshot skipped for %s: %s", trading_symbol, e)

    return result


def _fetch_live(yf_sym: str, result: dict) -> None:
    """Fill ``result`` from Yahoo. Raises YahooNoData or YahooUnavailable."""
    info = _fetch_info(yf_sym)

    # Statements are best-effort extras (ETFs have none at all), fetched one
    # after another through the gateway rather than in a parallel burst. Once
    # Yahoo starts refusing, the rest are skipped.
    ticker = yf.Ticker(yf_sym)
    fetched: dict = {}
    for name in _STATEMENTS:
        try:
            fetched[name] = yahoo_gateway.call(
                lambda name=name: getattr(ticker, name), what=name, symbol=yf_sym
            )
        except YahooUnavailable as e:
            logger.warning("yfinance %s for %s skipped: %s", name, yf_sym, e.cause)
            break
        except Exception as e:
            logger.warning("yfinance %s for %s failed: %s: %s", name, yf_sym, type(e).__name__, e)
            fetched[name] = None

    financials = fetched.get("financials")
    balance_sheet = fetched.get("balance_sheet")
    major_holders = fetched.get("major_holders")

    result["company_name"] = info.get("longName") or info.get("shortName", "")
    result["sector"] = info.get("sector", "")
    result["industry"] = info.get("industry", "")
    result["market_cap"] = info.get("marketCap")
    result["current_price"] = info.get("currentPrice") or info.get("regularMarketPrice")

    result["valuation"] = {
        "pe_ratio": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "pb_ratio": info.get("priceToBook"),
        "ev_ebitda": info.get("enterpriseToEbitda"),
        "peg_ratio": info.get("pegRatio"),
        "dividend_yield": _pct(info.get("dividendYield")),
        "trailing_eps": info.get("trailingEps"),
    }

    result["health"] = {
        "roe": _pct(info.get("returnOnEquity")),
        "debt_to_equity": info.get("debtToEquity"),
        "free_cash_flow": info.get("freeCashflow"),
        "operating_cash_flow": info.get("operatingCashflow"),
        "total_debt": info.get("totalDebt"),
        "total_cash": info.get("totalCash"),
        "revenue_growth": _pct(info.get("revenueGrowth")),
        "earnings_growth": _pct(info.get("earningsGrowth")),
        "profit_margin": _pct(info.get("profitMargins")),
        "operating_margin": _pct(info.get("operatingMargins")),
    }

    result["health"]["roce"] = _compute_roce(financials, balance_sheet)
    result["health"]["promoter_holding"] = _get_promoter_holding(major_holders)
    _add_financial_trends(financials, result)


def _fetch_info(yf_sym: str) -> dict:
    """Return ``Ticker.info`` with a quote in it, or raise.

    yfinance swallows HTTP errors on this path by default (``hide_exceptions``):
    a 401/403/429/5xx from Yahoo is logged to the ``yfinance`` logger and
    ``info`` comes back empty, indistinguishable from an unknown symbol. The
    gateway captures that hidden error; when there is none, an empty ``info``
    is checked against Yahoo's quote endpoint, which does raise, before it is
    believed.

    Raises :class:`YahooNoData` when Yahoo says the symbol doesn't exist and
    :class:`YahooUnavailable` when Yahoo failed. Retries once, and only for a
    timeout, network or 5xx error: retrying a rate limit makes it longer.
    """
    for attempt in range(1, _INFO_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(_RETRY_BACKOFF * (attempt - 1))
        try:
            # A fresh Ticker each attempt: yfinance memoises `info`, empty or not.
            info = yahoo_gateway.call(
                lambda: yf.Ticker(yf_sym).info or {}, what="info", symbol=yf_sym
            )
            if info.get("regularMarketPrice") is not None:
                return info
            hidden = yahoo_gateway.fail_from_swallowed("info", yf_sym)
            if isinstance(hidden, YahooNotFound):
                raise YahooNoData(yf_sym)
            if hidden is not None:
                raise hidden
            try:
                quoted = yahoo_gateway.call(
                    lambda: _yahoo_has_quote(yf_sym), what="quote", symbol=yf_sym
                )
            except YahooNotFound:
                quoted = False
            if not quoted:
                raise YahooNoData(yf_sym)
            detail = f"Yahoo quotes {yf_sym} but info came back empty (request likely blocked)"
            raise YahooUnavailable(
                yahoo_gateway.BLOCKED, detail,
                yahoo_gateway.record_failure(yahoo_gateway.BLOCKED, detail),
            )
        except YahooNotFound as e:
            raise YahooNoData(yf_sym) from e
        except YahooUnavailable as e:
            if e.cause not in _RETRYABLE_NOW or attempt == _INFO_ATTEMPTS:
                raise
            logger.warning(
                "yfinance info for %s failed (attempt %d/%d): %s: %s",
                yf_sym, attempt, _INFO_ATTEMPTS, e.cause, e.detail,
            )
    raise AssertionError("unreachable")  # pragma: no cover


def _yahoo_has_quote(yf_sym: str) -> bool:
    """Ask Yahoo's quote endpoint whether it has a price for the symbol.

    Goes through yfinance's shared session so it reuses the cookie, crumb and
    browser impersonation; unlike ``Ticker.info`` this raises on HTTP errors.
    """
    data = YfData().get_raw_json(
        _QUOTE_URL, params={"symbols": yf_sym, "formatted": "false"}, timeout=10
    )
    results = ((data or {}).get("quoteResponse") or {}).get("result") or []
    return any(
        r.get("symbol") == yf_sym and r.get("regularMarketPrice") is not None
        for r in results
    )


def _pct(val) -> Optional[float]:
    if val is None:
        return None
    return round(val * 100, 2)


def _compute_roce(financials, balance_sheet) -> Optional[float]:
    """ROCE = EBIT / Capital Employed (uses pre-fetched DataFrames)."""
    try:
        if financials is None or financials.empty:
            return None
        if balance_sheet is None or balance_sheet.empty:
            return None

        latest_col = balance_sheet.columns[0]

        ebit = None
        for label in ["EBIT", "Operating Income"]:
            if label in financials.index:
                ebit = financials.loc[label, latest_col]
                break
        if ebit is None:
            return None

        total_assets = None
        for label in ["Total Assets"]:
            if label in balance_sheet.index:
                total_assets = balance_sheet.loc[label, latest_col]
                break

        current_liabilities = None
        for label in ["Current Liabilities", "Total Current Liabilities"]:
            if label in balance_sheet.index:
                current_liabilities = balance_sheet.loc[label, latest_col]
                break

        if total_assets and current_liabilities:
            capital_employed = total_assets - current_liabilities
            if capital_employed > 0:
                return round((ebit / capital_employed) * 100, 2)
    except Exception as e:
        logger.debug("ROCE computation failed: %s", e)
    return None


def _get_promoter_holding(major_holders) -> Optional[float]:
    """Extract promoter/insider holding from pre-fetched DataFrame."""
    try:
        if major_holders is None or major_holders.empty:
            return None
        for _, row in major_holders.iterrows():
            label = str(row.iloc[1]).lower() if len(row) > 1 else ""
            if "insider" in label or "promoter" in label:
                val = row.iloc[0]
                if isinstance(val, str):
                    val = float(val.replace("%", ""))
                return round(float(val), 2)
    except Exception as e:
        logger.debug("Promoter holding fetch failed: %s", e)
    return None


def _add_financial_trends(financials, result: dict):
    """Extract multi-year revenue and profit data from pre-fetched DataFrame."""
    try:
        if financials is None or financials.empty:
            return

        revenue_trend = []
        profit_trend = []

        for col in reversed(financials.columns):
            year = str(col.year) if hasattr(col, "year") else str(col)[:4]

            revenue = None
            for label in ["Total Revenue", "Revenue"]:
                if label in financials.index:
                    val = financials.loc[label, col]
                    if val and val == val:
                        revenue = float(val)
                        break

            net_income = None
            for label in ["Net Income", "Net Income Common Stockholders"]:
                if label in financials.index:
                    val = financials.loc[label, col]
                    if val and val == val:
                        net_income = float(val)
                        break

            if revenue is not None:
                revenue_trend.append({"year": year, "value": revenue})
            if net_income is not None:
                profit_trend.append({"year": year, "value": net_income})

        result["revenue_trend"] = revenue_trend
        result["profit_trend"] = profit_trend

    except Exception as e:
        logger.debug("Financial trends extraction failed: %s", e)
