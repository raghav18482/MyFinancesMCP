import os
import sys

_project_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(_project_dir)
sys.path.insert(0, _project_dir)

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from mcp.server.fastmcp import FastMCP, Context

from session_manager import sessions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("AngelOne Portfolio", host="0.0.0.0")


# ── Helpers ────────────────────────────────────────────────────────────────


def _get_session_key(ctx: Context) -> str:
    """Derive a stable key that identifies the current MCP connection."""
    return str(id(ctx.session))


def _require_client(ctx: Context):
    """Return the AngelOneClient for this session, or raise."""
    client = sessions.get_client(_get_session_key(ctx))
    if client is None:
        raise RuntimeError(
            "Not logged in. Please call the 'login' tool first with your Angel One credentials."
        )
    return client


def _safe_call(fn, *args, **kwargs) -> str:
    try:
        result = fn(*args, **kwargs)
        if isinstance(result, dict) and not result.get("status"):
            return f"API Error: {result.get('message', json.dumps(result, indent=2))}"
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.exception("Tool call failed")
        return f"Error: {e}"


def _format_holdings_table(data: dict) -> str:
    if not data.get("status") or not data.get("data"):
        return f"API Error: {data.get('message', 'No holdings data')}"

    holdings = data["data"]
    if not holdings:
        return "No holdings found in your portfolio."

    lines = []
    total_invested = 0.0
    total_current = 0.0

    lines.append(f"{'Symbol':<20} {'Qty':>6} {'Avg Price':>10} {'LTP':>10} {'P&L':>12} {'P&L %':>8}")
    lines.append("-" * 70)

    for h in holdings:
        symbol = h.get("tradingsymbol", "N/A")
        qty = int(h.get("quantity", 0) or 0)
        avg_price = float(h.get("averageprice", 0) or 0)
        ltp = float(h.get("ltp", 0) or 0)
        invested = qty * avg_price
        current = qty * ltp
        pnl = current - invested
        pnl_pct = (pnl / invested * 100) if invested else 0.0

        total_invested += invested
        total_current += current

        lines.append(f"{symbol:<20} {qty:>6} {avg_price:>10.2f} {ltp:>10.2f} {pnl:>12.2f} {pnl_pct:>7.2f}%")

    total_pnl = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0.0

    lines.append("-" * 70)
    lines.append(f"Total Invested: Rs.{total_invested:,.2f}")
    lines.append(f"Current Value:  Rs.{total_current:,.2f}")
    lines.append(f"Total P&L:      Rs.{total_pnl:,.2f} ({total_pnl_pct:.2f}%)")

    return "\n".join(lines)


def _format_positions_table(data: dict) -> str:
    if not data.get("status") or not data.get("data"):
        return f"API Error: {data.get('message', 'No positions data')}"

    positions = data["data"]
    if not positions:
        return "No open positions."

    lines = []
    lines.append(f"{'Symbol':<20} {'Type':<6} {'Qty':>6} {'Buy Avg':>10} {'Sell Avg':>10} {'LTP':>10} {'P&L':>12}")
    lines.append("-" * 80)

    total_pnl = 0.0
    for p in positions:
        symbol = p.get("tradingsymbol", "N/A")
        product = p.get("producttype", "N/A")
        net_qty = int(p.get("netqty", 0) or 0)
        buy_avg = float(p.get("buyavgprice", 0) or 0)
        sell_avg = float(p.get("sellavgprice", 0) or 0)
        ltp = float(p.get("ltp", 0) or 0)
        pnl = float(p.get("pnl", 0) or 0)
        total_pnl += pnl

        lines.append(f"{symbol:<20} {product:<6} {net_qty:>6} {buy_avg:>10.2f} {sell_avg:>10.2f} {ltp:>10.2f} {pnl:>12.2f}")

    lines.append("-" * 80)
    lines.append(f"Total Day P&L: Rs.{total_pnl:,.2f}")

    return "\n".join(lines)


def _format_order_book(data: dict) -> str:
    if not data.get("status") or not data.get("data"):
        return f"API Error: {data.get('message', 'No order book data')}"

    orders = data["data"]
    if not orders:
        return "No orders placed today."

    lines = []
    lines.append(f"{'OrderID':<12} {'Symbol':<18} {'Type':<6} {'Qty':>6} {'Price':>10} {'Status':<12} {'Time':<10}")
    lines.append("-" * 80)

    for o in orders:
        lines.append(
            f"{o.get('orderid','N/A'):<12} "
            f"{o.get('tradingsymbol','N/A'):<18} "
            f"{o.get('transactiontype','N/A'):<6} "
            f"{int(o.get('quantity',0) or 0):>6} "
            f"{float(o.get('price',0) or 0):>10.2f} "
            f"{o.get('status','N/A'):<12} "
            f"{o.get('updatetime','N/A'):<10}"
        )

    return "\n".join(lines)


# ── Authentication ─────────────────────────────────────────────────────────


@mcp.tool()
def login(api_key: str, client_id: str, password: str, totp_secret: str, ctx: Context = None) -> str:
    """Log in to Angel One. Must be called before using any other tool.

    Credentials are held in memory only for this session and are never stored on disk.

    Args:
        api_key: Your Angel One API key
        client_id: Your Angel One client ID (e.g. "AB1234")
        password: Your Angel One PIN / password
        totp_secret: Your TOTP secret for 2FA (base32 string from Angel One)
    """
    try:
        session_key = _get_session_key(ctx)
        sessions.create_session(session_key, api_key, client_id, password, totp_secret)
        return f"Logged in successfully as {client_id}. You can now use all portfolio and trading tools."
    except Exception as e:
        logger.exception("Login failed")
        return f"Login failed: {e}"


@mcp.tool()
def logout(ctx: Context = None) -> str:
    """Log out and discard your session credentials from memory."""
    session_key = _get_session_key(ctx)
    sessions.remove_session(session_key)
    return "Logged out. Credentials removed from memory."


# ── Read-Only Tools ────────────────────────────────────────────────────────


@mcp.tool()
def get_profile(ctx: Context = None) -> str:
    """Get your Angel One account profile: name, client ID, email, enabled exchanges, and broker info."""
    client = _require_client(ctx)
    result = client.get_profile()
    if result.get("status") and result.get("data"):
        d = result["data"]
        lines = [
            f"Name:       {d.get('name', 'N/A')}",
            f"Client ID:  {d.get('clientcode', 'N/A')}",
            f"Email:      {d.get('email', 'N/A')}",
            f"Phone:      {d.get('mobileno', 'N/A')}",
            f"Broker:     {d.get('broker', 'N/A')}",
            f"Exchanges:  {', '.join(d.get('exchanges', []))}",
            f"Products:   {', '.join(d.get('products', []))}",
        ]
        return "\n".join(lines)
    return _safe_call(lambda: result)


@mcp.tool()
def get_holdings(ctx: Context = None) -> str:
    """Get all stock holdings in your Angel One demat account with quantity, average price, LTP, and P&L for each stock."""
    client = _require_client(ctx)
    data = client.get_holdings()
    return _format_holdings_table(data)


@mcp.tool()
def get_all_holdings(ctx: Context = None) -> str:
    """Get complete holdings overview including total investment value, current value, and overall P&L."""
    client = _require_client(ctx)
    return _safe_call(client.get_all_holdings)


@mcp.tool()
def get_positions(ctx: Context = None) -> str:
    """Get all open intraday and delivery positions with unrealized P&L."""
    client = _require_client(ctx)
    data = client.get_positions()
    return _format_positions_table(data)


@mcp.tool()
def get_order_book(ctx: Context = None) -> str:
    """Get all orders placed today with their current status (open, completed, rejected, cancelled)."""
    client = _require_client(ctx)
    data = client.get_order_book()
    return _format_order_book(data)


@mcp.tool()
def get_trade_book(ctx: Context = None) -> str:
    """Get all executed trades for today with fill prices and quantities."""
    client = _require_client(ctx)
    return _safe_call(client.get_trade_book)


@mcp.tool()
def get_funds(ctx: Context = None) -> str:
    """Get your account fund details: available margin, used margin, and net balance across segments."""
    client = _require_client(ctx)
    result = client.get_funds()
    if result.get("status") and result.get("data"):
        d = result["data"]
        lines = [
            f"Available Cash:     Rs.{d.get('availablecash', 'N/A')}",
            f"Available Margin:   Rs.{d.get('availableintradaypayin', 'N/A')}",
            f"Used Margin:        Rs.{d.get('utiliseddebits', 'N/A')}",
            f"Collateral:         Rs.{d.get('collateral', 'N/A')}",
            f"M2M Unrealised:     Rs.{d.get('m2munrealized', 'N/A')}",
            f"M2M Realised:       Rs.{d.get('m2mrealized', 'N/A')}",
            f"Net (cash + margin): Rs.{d.get('net', 'N/A')}",
        ]
        return "\n".join(lines)
    return _safe_call(lambda: result)


@mcp.tool()
def get_ltp(exchange: str, tradingsymbol: str, symboltoken: str, ctx: Context = None) -> str:
    """Get last traded price for a specific stock/instrument.

    Args:
        exchange: Exchange segment, e.g. "NSE", "BSE", "NFO", "MCX"
        tradingsymbol: Trading symbol, e.g. "RELIANCE-EQ", "SBIN-EQ", "INFY-EQ"
        symboltoken: Symbol token number from Angel One, e.g. "2885" for Reliance
    """
    client = _require_client(ctx)
    return _safe_call(client.get_ltp, exchange, tradingsymbol, symboltoken)


@mcp.tool()
def search_scrip(exchange: str, search_text: str, ctx: Context = None) -> str:
    """Search for a stock/instrument by name or symbol on a given exchange. Use this to find symbol tokens needed by other tools.

    Args:
        exchange: Exchange segment, e.g. "NSE", "BSE", "NFO", "MCX"
        search_text: Stock name or symbol to search for, e.g. "Reliance", "SBIN", "TCS"
    """
    client = _require_client(ctx)
    result = client.search_scrip(exchange, search_text)
    if result.get("status") and result.get("data"):
        lines = []
        for item in result["data"][:15]:
            lines.append(
                f"  {item['tradingsymbol']:<25} token: {item['symboltoken']:<10} exchange: {item['exchange']}"
            )
        return f"Found {len(result['data'])} results:\n" + "\n".join(lines)
    return _safe_call(lambda: result)


@mcp.tool()
def portfolio_summary(ctx: Context = None) -> str:
    """Get a high-level summary of your entire portfolio: total invested, current value, overall P&L, and day's P&L."""
    client = _require_client(ctx)

    summary_parts = []

    try:
        all_h = client.get_all_holdings()
        if all_h.get("status") and all_h.get("data"):
            d = all_h["data"]
            total_inv = float(d.get("totalholdingvalue", 0) or 0)
            total_cur = float(d.get("totalcurrentvalue", 0) or 0)
            total_pnl = float(d.get("totalpnlpercentage", 0) or 0)
            summary_parts.append("=== Holdings Summary ===")
            summary_parts.append(f"Total Investment: Rs.{total_inv:,.2f}")
            summary_parts.append(f"Current Value:    Rs.{total_cur:,.2f}")
            summary_parts.append(f"Overall P&L %:    {total_pnl:.2f}%")
    except Exception as e:
        summary_parts.append(f"Holdings error: {e}")

    try:
        positions = client.get_positions()
        if positions.get("status") and positions.get("data"):
            day_pnl = sum(float(p.get("pnl", 0) or 0) for p in positions["data"])
            summary_parts.append(f"\n=== Day Positions P&L ===")
            summary_parts.append(f"Day P&L: Rs.{day_pnl:,.2f}")
        else:
            summary_parts.append("\nNo open positions today.")
    except Exception as e:
        summary_parts.append(f"Positions error: {e}")

    try:
        funds = client.get_funds()
        if funds.get("status") and funds.get("data"):
            d = funds["data"]
            summary_parts.append(f"\n=== Funds ===")
            summary_parts.append(f"Available Cash: Rs.{d.get('availablecash', 'N/A')}")
            summary_parts.append(f"Net Value:      Rs.{d.get('net', 'N/A')}")
    except Exception as e:
        summary_parts.append(f"Funds error: {e}")

    return "\n".join(summary_parts) if summary_parts else "Could not fetch portfolio summary."


@mcp.tool()
def get_candle_data(
    exchange: str,
    symboltoken: str,
    interval: str,
    fromdate: str,
    todate: str,
    ctx: Context = None,
) -> str:
    """Get historical OHLC candle data for a stock.

    Args:
        exchange: Exchange segment, e.g. "NSE", "BSE", "NFO"
        symboltoken: Symbol token, e.g. "2885" for Reliance
        interval: Candle interval - ONE_MINUTE, FIVE_MINUTE, FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY
        fromdate: Start date in "YYYY-MM-DD HH:MM" format, e.g. "2024-01-01 09:15"
        todate: End date in "YYYY-MM-DD HH:MM" format, e.g. "2024-01-31 15:30"
    """
    client = _require_client(ctx)
    params = {
        "exchange": exchange,
        "symboltoken": symboltoken,
        "interval": interval,
        "fromdate": fromdate,
        "todate": todate,
    }
    return _safe_call(client.get_candle_data, params)


# ── New Convenience Tools ──────────────────────────────────────────────────


@mcp.tool()
def get_stock_history(symbol: str, days: int = 30, interval: str = "ONE_DAY", ctx: Context = None) -> str:
    """Get historical OHLCV price data for a stock using just its name/symbol.

    Automatically resolves the symbol token and computes the date range.

    Args:
        symbol: Stock name or trading symbol, e.g. "SBIN", "RELIANCE", "TCS"
        days: Number of past days of history to fetch (default 30, max 365)
        interval: Candle interval - ONE_MINUTE, FIVE_MINUTE, FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY (default ONE_DAY)
    """
    client = _require_client(ctx)

    days = max(1, min(days, 365))

    scrip = client.search_scrip("NSE", symbol)
    if not scrip.get("status") or not scrip.get("data"):
        return f"Could not find symbol '{symbol}' on NSE. Try a different name."

    match = scrip["data"][0]
    token = match["symboltoken"]
    tradingsymbol = match["tradingsymbol"]
    exchange = match["exchange"]

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
        return f"No candle data returned for {tradingsymbol}. API message: {result.get('message', 'unknown')}"

    candles = result["data"]
    lines = [f"=== {tradingsymbol} ({exchange}) — Last {days} days ({interval}) ===", ""]
    lines.append(f"{'Date':<22} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>12}")
    lines.append("-" * 78)

    for c in candles:
        ts = c[0] if isinstance(c[0], str) else str(c[0])
        date_str = ts[:19] if len(ts) >= 19 else ts
        lines.append(f"{date_str:<22} {c[1]:>10.2f} {c[2]:>10.2f} {c[3]:>10.2f} {c[4]:>10.2f} {c[5]:>12}")

    if len(candles) >= 2:
        first_close = candles[0][4]
        last_close = candles[-1][4]
        change = last_close - first_close
        change_pct = (change / first_close * 100) if first_close else 0
        lines.append("-" * 78)
        lines.append(f"Period Change: Rs.{change:,.2f} ({change_pct:+.2f}%)")

    return "\n".join(lines)


@mcp.tool()
def calculate_pnl(symbol: str, ctx: Context = None) -> str:
    """Calculate detailed P&L for a specific stock from your holdings and today's positions.

    Shows invested value, current value, unrealized P&L from holdings, and intraday P&L from positions.

    Args:
        symbol: Stock trading symbol to look up, e.g. "SBIN", "RELIANCE", "TCS"
    """
    client = _require_client(ctx)
    symbol_upper = symbol.upper().strip()

    holding_match = None
    position_matches = []

    try:
        holdings = client.get_holdings()
        if holdings.get("status") and holdings.get("data"):
            for h in holdings["data"]:
                ts = (h.get("tradingsymbol") or "").upper()
                if symbol_upper in ts:
                    holding_match = h
                    break
    except Exception as e:
        return f"Error fetching holdings: {e}"

    try:
        positions = client.get_positions()
        if positions.get("status") and positions.get("data"):
            for p in positions["data"]:
                ts = (p.get("tradingsymbol") or "").upper()
                if symbol_upper in ts:
                    position_matches.append(p)
    except Exception as e:
        return f"Error fetching positions: {e}"

    if not holding_match and not position_matches:
        return f"No holdings or positions found matching '{symbol}'. Make sure you hold this stock or have an open position."

    lines = [f"=== P&L Report: {symbol_upper} ===", ""]

    if holding_match:
        h = holding_match
        ts = h.get("tradingsymbol", "N/A")
        qty = int(h.get("quantity", 0) or 0)
        avg = float(h.get("averageprice", 0) or 0)
        ltp = float(h.get("ltp", 0) or 0)
        invested = qty * avg
        current = qty * ltp
        pnl = current - invested
        pnl_pct = (pnl / invested * 100) if invested else 0.0

        lines.append("── Holdings ──")
        lines.append(f"  Symbol:        {ts}")
        lines.append(f"  Quantity:      {qty}")
        lines.append(f"  Avg Price:     Rs.{avg:,.2f}")
        lines.append(f"  LTP:           Rs.{ltp:,.2f}")
        lines.append(f"  Invested:      Rs.{invested:,.2f}")
        lines.append(f"  Current Value: Rs.{current:,.2f}")
        lines.append(f"  Unrealized P&L: Rs.{pnl:,.2f} ({pnl_pct:+.2f}%)")
        lines.append("")

    if position_matches:
        lines.append("── Day Positions ──")
        total_day_pnl = 0.0
        for p in position_matches:
            ts = p.get("tradingsymbol", "N/A")
            product = p.get("producttype", "N/A")
            net_qty = int(p.get("netqty", 0) or 0)
            buy_avg = float(p.get("buyavgprice", 0) or 0)
            sell_avg = float(p.get("sellavgprice", 0) or 0)
            ltp = float(p.get("ltp", 0) or 0)
            pnl = float(p.get("pnl", 0) or 0)
            total_day_pnl += pnl

            lines.append(f"  {ts} ({product})")
            lines.append(f"    Net Qty: {net_qty}, Buy Avg: Rs.{buy_avg:,.2f}, Sell Avg: Rs.{sell_avg:,.2f}")
            lines.append(f"    LTP: Rs.{ltp:,.2f}, Day P&L: Rs.{pnl:,.2f}")

        if len(position_matches) > 1:
            lines.append(f"  Total Day P&L: Rs.{total_day_pnl:,.2f}")
        lines.append("")

    total_pnl = 0.0
    if holding_match:
        qty = int(holding_match.get("quantity", 0) or 0)
        avg = float(holding_match.get("averageprice", 0) or 0)
        ltp = float(holding_match.get("ltp", 0) or 0)
        total_pnl += (qty * ltp) - (qty * avg)
    for p in position_matches:
        total_pnl += float(p.get("pnl", 0) or 0)

    lines.append(f"── Combined P&L: Rs.{total_pnl:,.2f} ──")

    return "\n".join(lines)


@mcp.tool()
def get_market_depth(symbol: str, exchange: str = "NSE", ctx: Context = None) -> str:
    """Get full market depth (best 5 bids and asks) for a stock.

    Shows bid/ask prices, quantities, and order counts at each level plus OHLC and volume.

    Args:
        symbol: Stock name or trading symbol, e.g. "SBIN", "RELIANCE", "TCS"
        exchange: Exchange segment - NSE, BSE (default NSE)
    """
    client = _require_client(ctx)

    scrip = client.search_scrip(exchange, symbol)
    if not scrip.get("status") or not scrip.get("data"):
        return f"Could not find symbol '{symbol}' on {exchange}. Try a different name."

    match = scrip["data"][0]
    token = match["symboltoken"]
    tradingsymbol = match["tradingsymbol"]

    result = client.get_market_data("FULL", {exchange: [token]})
    if not result.get("status") or not result.get("data"):
        return f"Could not fetch market depth for {tradingsymbol}. API message: {result.get('message', 'unknown')}"

    data = result["data"]
    fetched = data.get("fetched", [{}])
    if not fetched:
        return f"No market data returned for {tradingsymbol}."

    stock = fetched[0]
    ltp = stock.get("ltp", "N/A")
    open_p = stock.get("open", "N/A")
    high = stock.get("high", "N/A")
    low = stock.get("low", "N/A")
    close = stock.get("close", "N/A")
    total_buy_qty = stock.get("totBuyQuan", "N/A")
    total_sell_qty = stock.get("totSellQuan", "N/A")

    lines = [f"=== {tradingsymbol} ({exchange}) Market Depth ===", ""]
    lines.append(f"LTP: Rs.{ltp}  |  Open: {open_p}  |  High: {high}  |  Low: {low}  |  Prev Close: {close}")
    lines.append("")

    depth = stock.get("depth", {})
    bids = depth.get("buy", [])
    asks = depth.get("sell", [])

    lines.append(f"{'':>5} {'BID':^32}  |  {'ASK':^32}")
    lines.append(f"{'Lvl':>5} {'Price':>10} {'Qty':>10} {'Orders':>8}  |  {'Price':>10} {'Qty':>10} {'Orders':>8}")
    lines.append("-" * 73)

    max_levels = max(len(bids), len(asks), 5)
    for i in range(max_levels):
        bid_price = f"{bids[i].get('price', 0):>10.2f}" if i < len(bids) else f"{'—':>10}"
        bid_qty = f"{bids[i].get('quantity', 0):>10}" if i < len(bids) else f"{'—':>10}"
        bid_ord = f"{bids[i].get('orders', 0):>8}" if i < len(bids) else f"{'—':>8}"
        ask_price = f"{asks[i].get('price', 0):>10.2f}" if i < len(asks) else f"{'—':>10}"
        ask_qty = f"{asks[i].get('quantity', 0):>10}" if i < len(asks) else f"{'—':>10}"
        ask_ord = f"{asks[i].get('orders', 0):>8}" if i < len(asks) else f"{'—':>8}"

        lines.append(f"{i+1:>5} {bid_price} {bid_qty} {bid_ord}  |  {ask_price} {ask_qty} {ask_ord}")

    lines.append("-" * 73)
    lines.append(f"Total Buy Qty: {total_buy_qty}  |  Total Sell Qty: {total_sell_qty}")

    return "\n".join(lines)


# ── Trading Tools ──────────────────────────────────────────────────────────


@mcp.tool()
def place_order(
    variety: str,
    tradingsymbol: str,
    symboltoken: str,
    transactiontype: str,
    exchange: str,
    ordertype: str,
    producttype: str,
    quantity: str,
    price: str = "0",
    triggerprice: str = "0",
    duration: str = "DAY",
    ctx: Context = None,
) -> str:
    """Place a new order on Angel One. IMPORTANT: This will execute a real trade with real money.

    Args:
        variety: Order variety - NORMAL, STOPLOSS, AMO, ROBO
        tradingsymbol: Trading symbol, e.g. "SBIN-EQ"
        symboltoken: Symbol token, e.g. "3045"
        transactiontype: BUY or SELL
        exchange: Exchange - NSE, BSE, NFO, MCX
        ordertype: Order type - MARKET, LIMIT, STOPLOSS_LIMIT, STOPLOSS_MARKET
        producttype: Product type - DELIVERY, INTRADAY, CARRYFORWARD, MARGIN, BO
        quantity: Number of shares/lots to trade
        price: Limit price (use "0" for market orders)
        triggerprice: Trigger price for stop-loss orders (use "0" if not applicable)
        duration: DAY or IOC (Immediate or Cancel)
    """
    client = _require_client(ctx)
    order_params = {
        "variety": variety,
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": transactiontype,
        "exchange": exchange,
        "ordertype": ordertype,
        "producttype": producttype,
        "duration": duration,
        "price": price,
        "triggerprice": triggerprice,
        "quantity": quantity,
    }
    result = client.place_order(order_params)
    if result:
        return f"Order placed successfully. Order ID: {result}"
    return "Order placement failed. Check logs for details."


@mcp.tool()
def modify_order(
    variety: str,
    orderid: str,
    ordertype: str,
    quantity: str,
    price: str,
    triggerprice: str = "0",
    producttype: str = "DELIVERY",
    duration: str = "DAY",
    ctx: Context = None,
) -> str:
    """Modify an existing open order.

    Args:
        variety: Order variety - NORMAL, STOPLOSS, AMO, ROBO
        orderid: The order ID to modify
        ordertype: New order type - MARKET, LIMIT, STOPLOSS_LIMIT, STOPLOSS_MARKET
        quantity: New quantity
        price: New limit price
        triggerprice: New trigger price (use "0" if not applicable)
        producttype: Product type - DELIVERY, INTRADAY, CARRYFORWARD, MARGIN, BO
        duration: DAY or IOC
    """
    client = _require_client(ctx)
    order_params = {
        "variety": variety,
        "orderid": orderid,
        "ordertype": ordertype,
        "producttype": producttype,
        "duration": duration,
        "price": price,
        "triggerprice": triggerprice,
        "quantity": quantity,
    }
    return _safe_call(client.modify_order, order_params)


@mcp.tool()
def cancel_order(orderid: str, variety: str = "NORMAL", ctx: Context = None) -> str:
    """Cancel an open order by its order ID.

    Args:
        orderid: The order ID to cancel
        variety: Order variety - NORMAL, STOPLOSS, AMO, ROBO
    """
    client = _require_client(ctx)
    return _safe_call(client.cancel_order, orderid, variety)


if __name__ == "__main__":
    mcp.run(transport="sse")
