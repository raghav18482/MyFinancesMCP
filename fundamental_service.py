import time
import logging
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf

logger = logging.getLogger(__name__)

_cache: dict[str, dict] = {}
_CACHE_TTL = 6 * 3600  # 6 hours


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
    """Fetch full fundamental analysis — heavy yfinance properties
    are loaded in parallel threads to cut latency by ~3x.
    """
    cache_key = f"fundamental:{trading_symbol}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    yf_sym = _yf_symbol(trading_symbol)
    result: dict = {
        "symbol": trading_symbol,
        "yf_symbol": yf_sym,
        "valuation": {},
        "health": {},
        "revenue_trend": [],
        "profit_trend": [],
        "error": None,
    }

    try:
        ticker = yf.Ticker(yf_sym)

        # Fetch all expensive properties in parallel
        fetched: dict = {}
        props = {
            "info": lambda: ticker.info or {},
            "financials": lambda: ticker.financials,
            "balance_sheet": lambda: ticker.balance_sheet,
            "major_holders": lambda: ticker.major_holders,
        }

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fn): name for name, fn in props.items()}
            for future in futures:
                name = futures[future]
                try:
                    fetched[name] = future.result(timeout=15)
                except Exception as e:
                    logger.debug("yfinance property '%s' failed: %s", name, e)
                    fetched[name] = None

        info = fetched.get("info") or {}
        financials = fetched.get("financials")
        balance_sheet = fetched.get("balance_sheet")
        major_holders = fetched.get("major_holders")

        if not info or info.get("regularMarketPrice") is None:
            result["error"] = f"No data found for {yf_sym}"
            return result

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

    except Exception as e:
        logger.warning("Fundamental fetch failed for %s: %s", trading_symbol, e)
        result["error"] = str(e)

    _cache_set(cache_key, result)
    return result


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
