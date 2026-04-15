import os
import sys

_project_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(_project_dir)
sys.path.insert(0, _project_dir)

import json
import logging

from mcp.server.fastmcp import FastMCP, Context

from session_manager import sessions
from services.broker_service import (
    calculate_symbol_pnl,
    cancel_order_result,
    fetch_market_depth,
    fetch_stock_history_candles,
    modify_order_result,
    place_order_result,
    portfolio_summary_as_dict,
)

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


def _format_portfolio_summary_from_dict(d: dict) -> str:
    parts = []
    hs = d.get("holdings_summary")
    if hs:
        parts.append("=== Holdings Summary ===")
        parts.append(f"Total Investment: Rs.{hs['total_investment']:,.2f}")
        parts.append(f"Current Value:    Rs.{hs['current_value']:,.2f}")
        parts.append(f"Overall P&L %:    {hs['overall_pnl_percent']:.2f}%")
    for err in d.get("errors", []):
        if err.get("section") == "holdings" and not hs:
            parts.append(f"Holdings error: {err.get('message', '')}")

    dp = d.get("day_positions")
    if dp:
        parts.append("\n=== Day Positions P&L ===")
        if dp.get("note") == "no_open_positions":
            parts.append("No open positions today.")
        else:
            parts.append(f"Day P&L: Rs.{dp['day_pnl']:,.2f}")
    for err in d.get("errors", []):
        if err.get("section") == "positions":
            parts.append(f"Positions error: {err.get('message', '')}")

    fd = d.get("funds")
    if fd:
        parts.append("\n=== Funds ===")
        parts.append(f"Available Cash: Rs.{fd.get('available_cash', 'N/A')}")
        parts.append(f"Net Value:      Rs.{fd.get('net', 'N/A')}")
    for err in d.get("errors", []):
        if err.get("section") == "funds" and not fd:
            parts.append(f"Funds error: {err.get('message', '')}")

    return "\n".join(parts) if parts else "Could not fetch portfolio summary."


def _format_pnl_from_dict(d: dict) -> str:
    symbol_upper = d["symbol"]
    lines = [f"=== P&L Report: {symbol_upper} ===", ""]
    hb = d.get("holding")
    if hb:
        lines.append("── Holdings ──")
        lines.append(f"  Symbol:        {hb['tradingsymbol']}")
        lines.append(f"  Quantity:      {hb['quantity']}")
        lines.append(f"  Avg Price:     Rs.{hb['average_price']:,.2f}")
        lines.append(f"  LTP:           Rs.{hb['ltp']:,.2f}")
        lines.append(f"  Invested:      Rs.{hb['invested']:,.2f}")
        lines.append(f"  Current Value: Rs.{hb['current_value']:,.2f}")
        lines.append(
            f"  Unrealized P&L: Rs.{hb['unrealized_pnl']:,.2f} ({hb['unrealized_pnl_percent']:+.2f}%)"
        )
        lines.append("")
    pblocks = d.get("positions") or []
    if pblocks:
        lines.append("── Day Positions ──")
        total_day = 0.0
        for p in pblocks:
            pnl = p["day_pnl"]
            total_day += pnl
            lines.append(f"  {p['tradingsymbol']} ({p['producttype']})")
            lines.append(
                f"    Net Qty: {p['netqty']}, Buy Avg: Rs.{p['buyavgprice']:,.2f}, "
                f"Sell Avg: Rs.{p['sellavgprice']:,.2f}"
            )
            lines.append(f"    LTP: Rs.{p['ltp']:,.2f}, Day P&L: Rs.{pnl:,.2f}")
        if len(pblocks) > 1:
            lines.append(f"  Total Day P&L: Rs.{total_day:,.2f}")
        lines.append("")
    lines.append(f"── Combined P&L: Rs.{d['combined_pnl_estimate']:,.2f} ──")
    return "\n".join(lines)


def _format_stock_history_from_candles(d: dict) -> str:
    if not d.get("ok"):
        return d.get("error", "Unknown error")
    tradingsymbol = d["tradingsymbol"]
    exchange = d["exchange"]
    days = d["days"]
    interval = d["interval"]
    candles = d["candles"]
    lines = [
        f"=== {tradingsymbol} ({exchange}) — Last {days} days ({interval}) ===",
        "",
    ]
    lines.append(f"{'Date':<22} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>12}")
    lines.append("-" * 78)
    for c in candles:
        ts = c[0] if isinstance(c[0], str) else str(c[0])
        date_str = ts[:19] if len(ts) >= 19 else ts
        lines.append(
            f"{date_str:<22} {c[1]:>10.2f} {c[2]:>10.2f} {c[3]:>10.2f} {c[4]:>10.2f} {c[5]:>12}"
        )
    summ = d.get("summary")
    if summ:
        lines.append("-" * 78)
        lines.append(
            f"Period Change: Rs.{summ['period_change']:,.2f} ({summ['period_change_percent']:+.2f}%)"
        )
    return "\n".join(lines)


def _format_market_depth_from_dict(d: dict) -> str:
    if not d.get("ok"):
        return d.get("error", "Unknown error")
    tradingsymbol = d["tradingsymbol"]
    exchange = d["exchange"]
    lines = [f"=== {tradingsymbol} ({exchange}) Market Depth ===", ""]
    lines.append(
        f"LTP: Rs.{d.get('ltp')}  |  Open: {d.get('open')}  |  High: {d.get('high')}  "
        f"|  Low: {d.get('low')}  |  Prev Close: {d.get('close')}"
    )
    lines.append("")
    depth = d.get("depth") or {}
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
    lines.append(f"Total Buy Qty: {d.get('tot_buy_qty')}  |  Total Sell Qty: {d.get('tot_sell_qty')}")
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
    return _format_portfolio_summary_from_dict(portfolio_summary_as_dict(client))


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
    raw = fetch_stock_history_candles(client, symbol, days, interval)
    if not raw.get("ok"):
        msg = raw.get("error", "unknown")
        if raw.get("tradingsymbol"):
            return f"No candle data returned for {raw['tradingsymbol']}. API message: {msg}"
        return msg if isinstance(msg, str) else str(msg)
    return _format_stock_history_from_candles(raw)


@mcp.tool()
def calculate_pnl(symbol: str, ctx: Context = None) -> str:
    """Calculate detailed P&L for a specific stock from your holdings and today's positions.

    Shows invested value, current value, unrealized P&L from holdings, and intraday P&L from positions.

    Args:
        symbol: Stock trading symbol to look up, e.g. "SBIN", "RELIANCE", "TCS"
    """
    client = _require_client(ctx)
    d = calculate_symbol_pnl(client, symbol)
    if not d.get("ok"):
        err = d.get("error", "Unknown error")
        if err.startswith("holdings:") or err.startswith("positions:"):
            return f"Error fetching {err.split(':', 1)[0]}: {err.split(':', 1)[1].strip()}"
        return err
    return _format_pnl_from_dict(d)


@mcp.tool()
def get_market_depth(symbol: str, exchange: str = "NSE", ctx: Context = None) -> str:
    """Get full market depth (best 5 bids and asks) for a stock.

    Shows bid/ask prices, quantities, and order counts at each level plus OHLC and volume.

    Args:
        symbol: Stock name or trading symbol, e.g. "SBIN", "RELIANCE", "TCS"
        exchange: Exchange segment - NSE, BSE (default NSE)
    """
    client = _require_client(ctx)
    d = fetch_market_depth(client, symbol, exchange)
    if not d.get("ok") and d.get("tradingsymbol") and "Could not fetch" not in str(d.get("error", "")):
        return f"Could not fetch market depth for {d['tradingsymbol']}. API message: {d.get('error', 'unknown')}"
    return _format_market_depth_from_dict(d)


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
    pr = place_order_result(client, order_params)
    if pr.get("ok"):
        return f"Order placed successfully. Order ID: {pr.get('order_id')}"
    return pr.get("error", "Order placement failed. Check logs for details.")


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
    return json.dumps(modify_order_result(client, order_params), indent=2, default=str)


@mcp.tool()
def cancel_order(orderid: str, variety: str = "NORMAL", ctx: Context = None) -> str:
    """Cancel an open order by its order ID.

    Args:
        orderid: The order ID to cancel
        variety: Order variety - NORMAL, STOPLOSS, AMO, ROBO
    """
    client = _require_client(ctx)
    return json.dumps(cancel_order_result(client, orderid, variety), indent=2, default=str)


@mcp.tool()
def predict_price_direction(
    tradingsymbol: str,
    exchange: str = "NSE",
    days: int = 365,
    ctx: Context = None,
) -> str:
    """Predict whether a stock's price will go UP or DOWN across 7 timeframes.

    Uses technical analysis indicators (RSI, MACD, Bollinger Bands, ADX, etc.)
    and price-action features to generate predictions for:
    - 10 minutes, 1 hour, 4 hours, 1 day, 1 week, 1 month, 1 year

    Returns direction (UP/DOWN/NEUTRAL) with confidence percentage for each timeframe,
    plus an overall outlook and the top bullish/bearish signals driving the prediction.

    Args:
        tradingsymbol: Stock symbol, e.g. "RELIANCE-EQ" or "SBIN-EQ"
        exchange: Exchange - NSE or BSE
        days: Number of days of historical data to analyze (default 90)
    """
    from datetime import datetime, timedelta
    from services.prediction_service import predict_direction as _predict

    client = _require_client(ctx)
    search_key = tradingsymbol.split("-")[0] if "-" in tradingsymbol else tradingsymbol
    sr = _safe_call(client.search_scrip, exchange, search_key)
    if isinstance(sr, str):
        return f"Could not find symbol: {sr}"

    rows = sr.get("data") or []
    token = None
    for r in rows:
        ts = r.get("tradingsymbol", "")
        if ts == tradingsymbol or ts == search_key or ts == f"{search_key}-EQ":
            token = r.get("symboltoken")
            break
    if not token and rows:
        token = rows[0].get("symboltoken")
    if not token:
        return f"Could not find token for {tradingsymbol}"

    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days)
    candle_result = _safe_call(
        client.get_candle_data,
        {
            "exchange": exchange,
            "symboltoken": token,
            "interval": "ONE_DAY",
            "fromdate": from_dt.strftime("%Y-%m-%d 09:15"),
            "todate": to_dt.strftime("%Y-%m-%d 15:30"),
        },
    )
    if isinstance(candle_result, str):
        return f"Could not fetch candle data: {candle_result}"
    if not candle_result.get("status") or not candle_result.get("data"):
        return "No candle data available for this symbol."

    result = _predict(candle_result["data"], tradingsymbol)
    if result.get("error"):
        return result["error"]

    lines = [
        f"=== Price Prediction: {tradingsymbol} ({exchange}) ===",
        f"Overall Outlook: {result['overall_outlook'].upper()} (score: {result['overall_score']:+.3f})",
        f"Model: {result['model_type']}",
        "",
        f"{'Timeframe':<12} {'Direction':<10} {'Confidence':<12}",
        "-" * 36,
    ]
    for tf in ["10min", "1hr", "4hr", "1day", "1week", "1month", "1year"]:
        p = result["predictions"].get(tf, {})
        direction = p.get("direction", "N/A").upper()
        confidence = f"{p.get('confidence', 0) * 100:.1f}%"
        lines.append(f"{tf:<12} {direction:<10} {confidence:<12}")

    lines.append("")
    if result.get("top_bullish_signals"):
        lines.append("Bullish Signals:")
        for s in result["top_bullish_signals"]:
            lines.append(f"  + {s['feature']}: {s['value']:+.4f}")
    if result.get("top_bearish_signals"):
        lines.append("Bearish Signals:")
        for s in result["top_bearish_signals"]:
            lines.append(f"  - {s['feature']}: {s['value']:.4f}")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="sse")
