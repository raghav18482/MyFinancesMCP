import time
import logging
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import yfinance as yf
from yfinance.data import YfData

from services import fundamental_store
from services.fundamental_scoring import score_fundamentals

logger = logging.getLogger(__name__)

_cache: dict[str, dict] = {}
_CACHE_TTL = 6 * 3600  # 6 hours

_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
_INFO_ATTEMPTS = 2
_RETRY_BACKOFF = 1.5  # seconds, multiplied by the attempt number
_INFO_TIMEOUT = 45  # covers both attempts; yfinance's own per-request timeout is 30s
_AUX_TIMEOUT = 15


class YahooNoData(Exception):
    """Yahoo answered and has nothing for this symbol (unlisted on Yahoo)."""


class YahooFetchError(Exception):
    """Yahoo could not be reached or refused us (rate limit, 401/403, 5xx, timeout).

    Transient: the caller should offer a retry instead of "not available".
    """


def _cache_get(key: str) -> Optional[dict]:
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: str, data: dict):
    _cache[key] = {"data": data, "ts": time.time()}


def _yf_symbol(trading_symbol: str) -> str:
    base = trading_symbol.replace("-EQ", "").replace("-BE", "")
    return f"{base}.NS"


def get_stock_fundamentals(trading_symbol: str) -> dict:
    """Fundamentals for one stock: from the store if fetched already, else live.

    Yahoo refuses requests from the production server's IPs, so production is
    served from ``fundamental_store``, which a scheduled GitHub Actions job
    keeps filled (``scripts/refresh_fundamentals.py``). A stock that isn't
    stored yet is fetched live, which works from a residential IP. If Yahoo
    refuses, the stock is queued for the job and the caller gets a retryable
    "being fetched" answer.
    """
    cache_key = f"fundamental:{trading_symbol}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    stored = fundamental_store.load(trading_symbol)
    if stored is not None:
        _cache_set(cache_key, stored)
        return stored

    # Already queued: don't ask Yahoo again from a server it refuses.
    if fundamental_store.requested_recently(trading_symbol):
        return _being_fetched(trading_symbol)

    result = fetch_live(trading_symbol)
    if result.get("retryable"):
        return _being_fetched(trading_symbol) if fundamental_store.request(trading_symbol) else result
    if result["error"] is not None:
        return result  # Yahoo has no data for it; not cached, see fetch_live

    _cache_set(cache_key, result)
    fundamental_store.save(result)

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


def drop_cached(keys: set[str]) -> None:
    """Forget cached results for these stocks (base symbols) after a refresh."""
    for cache_key in list(_cache):
        if fundamental_store.key(cache_key.split(":", 1)[-1]) in keys:
            _cache.pop(cache_key, None)


def _empty_result(trading_symbol: str) -> dict:
    return {
        "symbol": trading_symbol,
        "yf_symbol": _yf_symbol(trading_symbol),
        "valuation": {},
        "health": {},
        "revenue_trend": [],
        "profit_trend": [],
        "score": None,
        "error": None,
    }


def _being_fetched(trading_symbol: str) -> dict:
    result = _empty_result(trading_symbol)
    result["error"] = (
        f"Fundamentals for {result['yf_symbol']} are being fetched in the background. "
        "Please check back in an hour or two."
    )
    result["retryable"] = True
    result["score"] = score_fundamentals(result)
    return result


def fetch_live(trading_symbol: str) -> dict:
    """Fetch full fundamental analysis from Yahoo, uncached and unstored.

    Heavy yfinance properties are loaded in parallel threads to cut latency
    by ~3x. ``error`` is None on success; a transient failure also sets
    ``retryable``. The scheduled job calls this directly.
    """
    result = _empty_result(trading_symbol)
    yf_sym = result["yf_symbol"]

    try:
        ticker = yf.Ticker(yf_sym)

        # Fetch all expensive properties in parallel. Only `info` is required;
        # the statements are best-effort extras (ETFs have none at all).
        fetched: dict = {}
        props = {
            "financials": lambda: ticker.financials,
            "balance_sheet": lambda: ticker.balance_sheet,
            "major_holders": lambda: ticker.major_holders,
        }

        with ThreadPoolExecutor(max_workers=4) as pool:
            info_future = pool.submit(_fetch_info, yf_sym)
            futures = {pool.submit(fn): name for name, fn in props.items()}
            for future in futures:
                name = futures[future]
                try:
                    fetched[name] = future.result(timeout=_AUX_TIMEOUT)
                except Exception as e:
                    logger.warning("yfinance %s for %s failed: %s: %s", name, yf_sym, type(e).__name__, e)
                    fetched[name] = None
            try:
                info = info_future.result(timeout=_INFO_TIMEOUT)
            except FutureTimeout as e:
                raise YahooFetchError(f"timed out after {_INFO_TIMEOUT}s") from e

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

    except YahooNoData:
        # Not cached: a symbol Yahoo adds later should show up without a 6h wait.
        result["error"] = f"No data found for {yf_sym}"
        result["score"] = score_fundamentals(result)
        return result
    except Exception as e:
        # Transient — never cached, so the next request (or Retry) tries again.
        cause = e.__cause__ or e
        logger.warning(
            "Fundamental fetch failed for %s (%s): %s: %s",
            trading_symbol, yf_sym, type(cause).__name__, cause,
        )
        result["error"] = f"Couldn't reach Yahoo Finance for {yf_sym}. Please try again shortly."
        result["retryable"] = True
        result["score"] = score_fundamentals(result)
        return result

    # Scored last so the 0-100 breakdown is stored and cached with the figures,
    # and so the agent tool and the research API both get it without extra work.
    result["score"] = score_fundamentals(result)
    return result


def _fetch_info(yf_sym: str) -> dict:
    """Return ``Ticker.info`` with a quote in it, or raise.

    yfinance swallows HTTP errors on this path by default (``hide_exceptions``):
    a 401/403/5xx from Yahoo is logged to the ``yfinance`` logger and ``info``
    comes back empty, indistinguishable from an unknown symbol. So an empty
    ``info`` is checked against Yahoo's quote endpoint directly, which does
    raise, before it is believed.

    Raises :class:`YahooNoData` when Yahoo says the symbol doesn't exist, and
    :class:`YahooFetchError` (chained to the real cause) when Yahoo failed.
    """
    last_error: Optional[BaseException] = None
    for attempt in range(1, _INFO_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(_RETRY_BACKOFF * (attempt - 1))
        try:
            # A fresh Ticker each attempt: yfinance memoises `info`, empty or not.
            info = yf.Ticker(yf_sym).info or {}
            if info.get("regularMarketPrice") is not None:
                return info
            if not _yahoo_has_quote(yf_sym):
                raise YahooNoData(yf_sym)
            last_error = YahooFetchError(
                f"Yahoo quotes {yf_sym} but Ticker.info came back empty (request likely blocked)"
            )
        except YahooNoData:
            raise
        except Exception as e:  # YFRateLimitError, HTTPError, timeouts, crumb failures
            last_error = e
        logger.warning(
            "yfinance info for %s failed (attempt %d/%d): %s: %s",
            yf_sym, attempt, _INFO_ATTEMPTS, type(last_error).__name__, last_error,
        )
    raise YahooFetchError(str(last_error)) from last_error


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
