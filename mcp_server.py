"""
Read-only Angel One MCP server (Streamable HTTP, bearer-token authenticated).

Every request must carry ``Authorization: Bearer <token>``, where the token is
a personal access token the user created in the web app (``/connect``). These
are custom tokens verified against the database — **not OAuth**: there is no
authorization server, consent flow or client registration here, and none is
advertised. The server runs stateless, so each HTTP request is authenticated
on its own and there is no MCP session id to hijack; revoking a token takes
effect on the next request.

Identity comes only from the verified token. No tool accepts a session, user,
account or credential argument, and broker credentials never pass through the
model: the server rebuilds the user's broker session from their encrypted
stored credentials.

The tool surface is read-only and fixed: :data:`ALLOWED_MCP_TOOLS` is checked
at import and the server refuses to start if anything else is registered.
Tools only ever hold a :class:`services.readonly_broker.ReadOnlyBroker`.
"""
import os
import sys

_project_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(_project_dir)
sys.path.insert(0, _project_dir)

import contextvars
import json
import logging
import re

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings

from adminApi import service as admin_service
from agents.readonly_guard import FORBIDDEN_TOOL_RE
from services import mcp_tokens
from services.broker_service import (
    calculate_symbol_pnl,
    fetch_market_depth,
    fetch_stock_history_candles,
    portfolio_summary_as_dict,
)
from services.readonly_broker import ReadOnlyBroker
from session_manager import sessions
from web import security_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PUBLIC_BASE_URL = security_config.public_base_url()

# The exact, read-only tool surface. Nothing here places, modifies or cancels
# an order, and there are no login/logout tools: authentication is the bearer
# token, never credentials typed into a tool.
ALLOWED_MCP_TOOLS: frozenset[str] = frozenset({
    "get_profile",
    "get_holdings",
    "get_all_holdings",
    "get_positions",
    "get_order_book",
    "get_trade_book",
    "get_funds",
    "get_ltp",
    "search_scrip",
    "portfolio_summary",
    "get_candle_data",
    "get_stock_history",
    "calculate_pnl",
    "get_market_depth",
    "predict_price_direction",
})

_USER_CLIENT_ID_RE = re.compile(r"user:(\d{1,12})")

# Set per request by :class:`ClientIPMiddleware`; read by the token verifier,
# which only receives the token, so that failed attempts can be limited per IP.
_client_ip_var: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_client_ip", default="unknown")


class ClientIPMiddleware:
    """ASGI middleware recording the caller's IP for token rate limiting.

    Honours one proxy hop (``X-Forwarded-For``), matching ``web.dependencies.client_ip``.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            ip = "unknown"
            for name, value in scope.get("headers") or []:
                if name == b"x-forwarded-for":
                    ip = value.decode("latin-1").split(",")[0].strip() or ip
                    break
            else:
                client = scope.get("client")
                if client:
                    ip = client[0]
            token = _client_ip_var.set(ip)
            try:
                await self.app(scope, receive, send)
            finally:
                _client_ip_var.reset(token)
            return
        await self.app(scope, receive, send)


class DbTokenVerifier:
    """FastMCP ``TokenVerifier`` backed by :mod:`services.mcp_tokens`."""

    async def verify_token(self, token: str) -> AccessToken | None:
        ip = _client_ip_var.get()
        verified = await anyio.to_thread.run_sync(mcp_tokens.verify, token, ip)
        if verified is None:
            return None
        return AccessToken(
            # The raw token is never carried past this point.
            token="redacted",
            client_id=f"user:{verified.user_id}",
            scopes=list(verified.scopes),
            expires_at=verified.expires_at,
        )


def _transport_security() -> TransportSecuritySettings:
    """DNS-rebinding protection keyed to ``ALLOWED_HOSTS``.

    With an explicit host list, the MCP endpoint checks ``Host`` and ``Origin``
    against it (on top of the outer ``TrustedHostMiddleware``). With no list —
    local development only; production refuses to start without one — the
    check is off, matching the web app's "accept any Host" in that case.
    """
    hosts = security_config.allowed_hosts()
    if "*" in hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    allowed_hosts: list[str] = []
    allowed_origins: list[str] = []
    for h in hosts:
        allowed_hosts += [h, f"{h}:*"]
        schemes = ("http", "https") if h in ("localhost", "127.0.0.1") else ("https",)
        for scheme in schemes:
            allowed_origins += [f"{scheme}://{h}", f"{scheme}://{h}:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


mcp = FastMCP(
    "AngelOne Portfolio (read-only)",
    instructions=(
        "Read-only access to the authenticated user's Angel One portfolio and market data. "
        "This server cannot place, modify or cancel orders. Present any trade idea as "
        "hypothetical analysis, never as an executed transaction."
    ),
    token_verifier=DbTokenVerifier(),
    auth=AuthSettings(
        # Required by the SDK when a verifier is set. No OAuth server runs here:
        # ``resource_server_url=None`` means no protected-resource metadata is
        # published, so OAuth-capable clients are not sent looking for one.
        issuer_url=PUBLIC_BASE_URL,
        resource_server_url=None,
        required_scopes=[mcp_tokens.SCOPE_PORTFOLIO_READ],
    ),
    stateless_http=True,
    streamable_http_path="/mcp",
    transport_security=_transport_security(),
)


# ── Identity ───────────────────────────────────────────────────────────────


class NotAuthorized(PermissionError):
    """The request has no verified identity, or it does not map to a usable account."""


def _verified_user_id() -> int:
    """The user id from this request's verified bearer token. Fails closed."""
    tok = get_access_token()
    if tok is None:
        raise NotAuthorized("Authentication required: send a valid MCP access token.")
    if mcp_tokens.SCOPE_PORTFOLIO_READ not in (tok.scopes or []):
        raise NotAuthorized("This token is not allowed to read portfolio data.")
    m = _USER_CLIENT_ID_RE.fullmatch(tok.client_id or "")
    if not m:
        raise NotAuthorized("Authentication required: send a valid MCP access token.")
    return int(m.group(1))


def _require_client(ctx: Context = None) -> ReadOnlyBroker:
    """The read-only broker for the verified caller, building it on first use.

    The session key is derived from the verified token only. The live client is
    checked against the user's own broker account, so a mis-filed session can
    never answer for someone else.
    """
    user_id = _verified_user_id()
    state = admin_service.session_state(user_id)
    if state is None or not state.is_active:
        raise NotAuthorized("This account is not active.")

    key = mcp_tokens.mcp_session_key(user_id)

    def _build():
        creds = admin_service.credentials_for_user(user_id)
        if creds is None:
            return None
        try:
            return sessions.create_session(
                key, creds.api_key, creds.client_id, creds.password, creds.totp_secret
            )
        except Exception:
            logger.exception("MCP: Angel One login failed for user id=%s", user_id)
            raise RuntimeError(
                "Could not connect to Angel One with your saved credentials. "
                "Re-enroll in the web app if they have changed."
            ) from None

    client = sessions.get_or_create(key, _build)
    if client is None:
        raise NotAuthorized("No broker account is linked to this user.")
    if client.client_id != state.angel_client_id:
        sessions.remove_session(key)
        logger.error("MCP: session %s did not match user id=%s; dropped", key, user_id)
        raise NotAuthorized("Session does not belong to this user.")
    return ReadOnlyBroker(client)


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
    try:
        sr = client.search_scrip(exchange, search_key)
    except Exception as e:
        logger.exception("predict_price_direction: search_scrip failed")
        return f"Could not find symbol: {e}"
    if not isinstance(sr, dict) or not sr.get("status"):
        return f"Could not find symbol {tradingsymbol}."

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
    try:
        candle_result = client.get_candle_data(
            {
                "exchange": exchange,
                "symboltoken": token,
                "interval": "ONE_DAY",
                "fromdate": from_dt.strftime("%Y-%m-%d 09:15"),
                "todate": to_dt.strftime("%Y-%m-%d 15:30"),
            }
        )
    except Exception as e:
        logger.exception("predict_price_direction: get_candle_data failed")
        return f"Could not fetch candle data: {e}"
    if not isinstance(candle_result, dict) or not candle_result.get("status") or not candle_result.get("data"):
        return "No candle data available for this symbol."

    # Interval passed explicitly: the predictor checks it against the model's
    # own record rather than assuming the two agree.
    result = _predict(candle_result["data"], tradingsymbol, interval="ONE_DAY")
    if result.get("error"):
        return result["error"]

    engine = result.get("model_type", "unknown")
    version = result.get("model_version")
    lines = [
        f"=== Price Prediction: {tradingsymbol} ({exchange}) ===",
        f"Overall Outlook: {result['overall_outlook'].upper()} (score: {result['overall_score']:+.3f})",
        f"Engine: {engine}" + (f" (version {version})" if version else ""),
    ]
    if result.get("disclaimer"):
        lines.append(f"NOTE: {result['disclaimer']}")
    lines += [
        "",
        f"{'Horizon':<10} {'Direction':<10} {'P(up)':<9} {'Confidence':<11}",
        "-" * 42,
    ]
    for tf, p in result.get("predictions", {}).items():
        prob = p.get("probability_up")
        lines.append(
            f"{tf:<10} {p.get('direction', 'n/a').upper():<10} "
            f"{(f'{prob:.1%}' if prob is not None else '-'):<9} "
            f"{p.get('confidence', 0) * 100:.1f}%"
        )

    if not result.get("calibrated"):
        lines.append("")
        lines.append("Confidence is NOT a calibrated probability for this engine.")

    lines.append("")
    for key, sign in (("top_bullish_signals", "+"), ("top_bearish_signals", "-")):
        signals = result.get(key) or []
        if signals:
            lines.append("Bullish drivers:" if sign == "+" else "Bearish drivers:")
            for s in signals:
                val = s.get("value")
                lines.append(f"  {sign} {s['feature']}: "
                             + (f"{val:+.4f}" if isinstance(val, (int, float)) else "n/a"))

    if result.get("feature_note"):
        lines += ["", f"Context: {result['feature_note']}"]

    return "\n".join(lines)


def _assert_tool_surface() -> None:
    """Refuse to start if the registered tools are not exactly the read-only allowlist."""
    names = {t.name for t in mcp._tool_manager.list_tools()}
    forbidden = sorted(n for n in names if FORBIDDEN_TOOL_RE.search(n))
    unexpected = sorted(names - ALLOWED_MCP_TOOLS)
    missing = sorted(ALLOWED_MCP_TOOLS - names)
    if forbidden or unexpected or missing:
        raise RuntimeError(
            "MCP tool surface is not the read-only allowlist: "
            f"forbidden={forbidden} unexpected={unexpected} missing={missing}"
        )


_assert_tool_surface()


if __name__ == "__main__":
    # Standalone: same authenticated Streamable HTTP endpoint (``/mcp``) as the
    # combined app in ``main.py`` serves at ``/mcp/mcp``.
    mcp.run(transport="streamable-http")
