"""The assistant is read-only, enforced in code at every layer.

  * **Source.** No order-placement code exists anywhere in the application.
  * **Registration.** MCP and ADK expose exactly their read-only allowlists.
  * **Execution.** A tool name outside the allowlist is refused server-side,
    even if a model invents it.
  * **Capability.** AI tools hold a ``ReadOnlyBroker``, which has no route to a
    mutation; hypothetical trade ideas store nothing and call only reads.
  * **Web/chat.** The approve endpoint and the chat ``APPROVE`` command are gone.

Fake brokers only; nothing here can reach Angel One.

Run directly (``python tests/test_readonly_boundary.py``) or under pytest.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _security_harness import MUTATION_NAMES, FakeBroker, harness, run_all  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIP_DIRS = {".venv", "venv", ".claude", "tests", "__pycache__", ".git", "node_modules",
              "models", "data", "logs"}


def _source_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f.endswith((".py", ".js", ".html")):
                yield os.path.join(root, f)


def test_no_order_mutation_code_exists_in_the_source():
    pattern = re.compile(
        r"placeOrder|modifyOrder|cancelOrder|place_order_result|modify_order_result|"
        r"cancel_order_result|execute_proposal|def place_order|def modify_order|"
        r"def cancel_order|TRADE_EXECUTION_ENABLED|/approve['\"]"
    )
    offenders = []
    for path in _source_files():
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh, 1):
                if pattern.search(line):
                    offenders.append(f"{os.path.relpath(path, REPO)}:{i}: {line.strip()}")
    assert not offenders, "order-mutation code found:\n" + "\n".join(offenders)
    print("ok  no order placement, modification, cancellation or execution code in the source")


def test_smart_api_is_private_to_the_broker_client():
    offenders = []
    for path in _source_files():
        if path.endswith(os.path.join("", "angel_client.py")) and os.path.dirname(path) == REPO:
            continue
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh, 1):
                if re.search(r"\b_?smart_api\b", line) and "FORBIDDEN_TOOL_RE" not in line \
                        and "smart_?api" not in line:
                    offenders.append(f"{os.path.relpath(path, REPO)}:{i}")
    assert not offenders, f"SmartConnect object referenced outside angel_client.py: {offenders}"

    from angel_client import AngelOneClient
    for name in ("place_order", "modify_order", "cancel_order", "smart_api"):
        assert not hasattr(AngelOneClient, name), name
    print("ok  the broker client has no order methods and keeps SmartConnect private")


def test_mcp_tool_surface_is_exactly_the_read_only_allowlist():
    import mcp_server
    from agents.readonly_guard import FORBIDDEN_TOOL_RE

    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == set(mcp_server.ALLOWED_MCP_TOOLS), names ^ set(mcp_server.ALLOWED_MCP_TOOLS)
    assert not [n for n in names if FORBIDDEN_TOOL_RE.search(n)]

    identity_params = re.compile(
        r"(session|session_id|sid|user|user_id|uid|client|client_id|account|account_id|"
        r"password|pin|totp|totp_secret|api_key|apikey|token|access_token|auth_token|bearer)",
        re.IGNORECASE,
    )
    for t in tools:
        props = (t.inputSchema or {}).get("properties", {})
        bad = [p for p in props if identity_params.fullmatch(p)]
        assert not bad, f"{t.name} takes identity/credential arguments: {bad}"
    print(f"ok  MCP exposes exactly {len(names)} read-only tools, none taking identity or credentials")


def test_removed_mcp_tools_are_rejected_server_side():
    import mcp_server
    from mcp.server.fastmcp.exceptions import ToolError

    for name, args in (
        ("place_order", {"variety": "NORMAL", "tradingsymbol": "INFY-EQ", "symboltoken": "1",
                         "transactiontype": "BUY", "exchange": "NSE", "ordertype": "MARKET",
                         "producttype": "DELIVERY", "quantity": "1"}),
        ("modify_order", {"orderid": "1"}),
        ("cancel_order", {"orderid": "1"}),
        ("login", {"api_key": "x", "client_id": "x", "password": "x", "totp_secret": "x"}),
        ("logout", {}),
        ("execute_proposal", {}),
    ):
        try:
            asyncio.run(mcp_server.mcp.call_tool(name, args))
        except ToolError as e:
            assert "Unknown tool" in str(e), e
        else:
            raise AssertionError(f"{name} was accepted")
    print("ok  calling a removed or invented MCP tool is an 'Unknown tool' error")


def test_mcp_refuses_to_start_with_an_extra_tool():
    import mcp_server

    @mcp_server.mcp.tool()
    def place_order() -> str:  # pragma: no cover — must never run
        return "nope"

    try:
        mcp_server._assert_tool_surface()
    except RuntimeError as e:
        assert "place_order" in str(e)
    else:
        raise AssertionError("an extra mutation tool did not stop startup")
    finally:
        mcp_server.mcp.remove_tool("place_order")
    mcp_server._assert_tool_surface()
    print("ok  the MCP startup check refuses a tool surface with a mutation tool on it")


def _agents(sid="sid-test"):
    from agents.finance.agent import build_finance_root_agent
    from agents.trading.agent import build_trading_root_agent

    return build_finance_root_agent(sid), build_trading_root_agent(sid)


def test_adk_agents_only_hold_allowlisted_read_only_tools():
    from agents.readonly_guard import ALLOWED_ADK_TOOLS, FORBIDDEN_TOOL_RE, tool_name

    for agent in _agents():
        names = {tool_name(t) for t in agent.tools}
        assert names == set(ALLOWED_ADK_TOOLS[agent.name]), (agent.name, names)
        assert not [n for n in names if FORBIDDEN_TOOL_RE.search(n)], names
        assert agent.before_tool_callback is not None
    print("ok  both ADK agents carry exactly their read-only allowlists and a guard callback")


def test_adk_refuses_to_build_an_agent_with_a_mutation_tool():
    from agents.factory import build_llm_agent
    from agents.readonly_guard import ToolNotPermitted

    def angel_place_order(symbol: str) -> dict:  # pragma: no cover — must never run
        return {}

    for name in ("finance_agent", "trading_agent", "some_new_agent"):
        try:
            build_llm_agent(name=name, instruction="x", tools=[angel_place_order])
        except ToolNotPermitted:
            pass
        else:
            raise AssertionError(f"{name} was built with angel_place_order")
    print("ok  building any agent with a mutation tool raises")


def test_adk_guard_blocks_an_invented_tool_call_at_execution_time():
    finance, trading = _agents()

    class _Tool:
        def __init__(self, name):
            self.name = name

    for agent in (finance, trading):
        cb = agent.before_tool_callback
        for invented in ("angel_place_order", "trading_execute", "cancel_order", "unknown_tool"):
            out = cb(tool=_Tool(invented), args={}, tool_context=None)
            assert isinstance(out, dict) and "not permitted" in out["error"], (agent.name, invented)
        # A real allowlisted tool passes through.
        allowed = next(iter({"finance_agent": {"angel_get_holdings"},
                             "trading_agent": {"trading_get_holdings"}}[agent.name]))
        assert cb(tool=_Tool(allowed), args={}, tool_context=None) is None
    print("ok  the execution-time guard refuses invented tool names and passes allowlisted ones")


def test_multi_user_agent_builds_need_an_explicit_session():
    from agents.finance.agent import build_finance_root_agent
    from agents.trading.agent import build_trading_root_agent

    prior = os.environ.get("ADK_ANGEL_SESSION_ID")
    os.environ["ADK_ANGEL_SESSION_ID"] = "shared-default-must-not-be-used"
    try:
        for build in (build_finance_root_agent, build_trading_root_agent):
            for sid in (None, "", "   "):
                try:
                    build(sid)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"{build.__name__}({sid!r}) fell back to a shared id")
    finally:
        if prior is None:
            os.environ.pop("ADK_ANGEL_SESSION_ID", None)
        else:
            os.environ["ADK_ANGEL_SESSION_ID"] = prior
    print("ok  agents for the web path refuse to build without the caller's own session id")


def test_read_only_broker_has_no_route_to_a_mutation():
    from services.readonly_broker import READ_ONLY_BROKER_METHODS, ReadOnlyBroker

    class Spy(FakeBroker):
        mutated = []

        def place_order(self, params):  # a raw client that *could* trade
            Spy.mutated.append(params)
            return "ORDER-1"

    raw = Spy("k", "AA1111", "p", "t")
    ro = ReadOnlyBroker(raw)
    for name in MUTATION_NAMES + ("__getattr__", "client", "_client", "api_key", "password",
                                  "totp_secret"):
        assert not hasattr(ro, name), name
    try:
        ro.place_order({"x": 1})
    except AttributeError:
        pass
    else:
        raise AssertionError("ReadOnlyBroker forwarded place_order")
    try:
        ro.extra = 1
    except AttributeError:
        pass
    else:
        raise AssertionError("ReadOnlyBroker accepted a new attribute")
    assert Spy.mutated == []

    public = {n for n in dir(ro) if not n.startswith("_")}
    assert public == set(READ_ONLY_BROKER_METHODS) | {"client_id", "client_code"}, public
    assert ro.get_holdings()["data"][0]["tradingsymbol"] == "INFY-EQ"
    assert ro.client_id == "AA1111"
    print("ok  ReadOnlyBroker exposes only allowlisted reads; mutations are AttributeErrors")


def test_every_ai_tool_receives_the_read_only_adapter():
    """Through the real session manager: the tools never see the raw client."""
    from agents.finance.tools_broker import make_broker_tools
    from agents.finance.tools_market import make_market_tools
    from agents.trading.tools import make_trading_tools
    from services import readonly_broker

    seen = []
    original = readonly_broker.wrap

    def spy_wrap(client):
        out = original(client)
        seen.append(type(out).__name__)
        return out

    with harness() as h:
        import session_manager
        session_manager.sessions.create_session("sid-ai", "k", "AA1111", "p", "t")
        readonly_broker.wrap = spy_wrap
        try:
            tools = {t.__name__: t for t in
                     make_broker_tools("sid-ai") + make_market_tools("sid-ai") +
                     make_trading_tools("sid-ai")}
            assert tools["angel_get_holdings"]()["data"][0]["tradingsymbol"] == "INFY-EQ"
            assert tools["trading_get_positions"]()["status"] is True
            assert tools["angel_portfolio_summary"]()
        finally:
            readonly_broker.wrap = original
    assert seen and set(seen) == {"ReadOnlyBroker"}, seen
    print("ok  ADK tools obtain their broker only through the read-only adapter")


def test_hypothetical_trade_idea_stores_nothing_and_only_reads():
    from agents.trading.tools import make_trading_tools
    from services.trade_proposals import proposal_store

    with harness():
        import session_manager
        client = session_manager.sessions.create_session("sid-idea", "k", "AA1111", "p", "t")
        before = dict(proposal_store._proposals)
        tools = {t.__name__: t for t in make_trading_tools("sid-idea")}
        idea = tools["trading_hypothetical_trade_idea"](
            tradingsymbol="INFY-EQ", transactiontype="BUY", quantity="5", price="100",
        )
        assert idea["hypothetical"] is True and idea["executable"] is False
        assert idea["label"] == "Hypothetical — not executed"
        assert "not been and cannot be executed" in idea["disclaimer"]
        assert idea["estimated_value"] == 500.0
        assert proposal_store._proposals == before, "a hypothetical idea created a proposal"
        assert all(c.startswith(("get_", "search_")) for c in client.calls), client.calls
        assert not hasattr(proposal_store, "approve") and not hasattr(proposal_store, "create")
    print("ok  a hypothetical idea is labelled, stores nothing, and makes no broker mutation")


def test_web_approval_and_chat_approve_are_gone():
    import web.routers.agent as agent_router

    with harness() as h:
        http = h.login("A")
        res = http.post("/api/trading/proposals/abcdef123456/approve")
        assert res.status_code in (404, 405), res.status_code

        captured = {}

        async def fake_chat(**kw):
            captured.update(kw)
            return {"reply": "This assistant is read-only."}

        original = agent_router.registry.chat
        agent_router.registry.chat = fake_chat
        try:
            r = http.post("/api/agent/chat", json={"message": "APPROVE abcdef12",
                                                   "agent_type": "trading"})
        finally:
            agent_router.registry.chat = original
        assert r.status_code == 200, r.text
        assert "approval_result" not in r.json()
        assert captured["message"] == "APPROVE abcdef12", "message must reach the agent unchanged"
        assert not hasattr(agent_router, "_try_handle_approval")
    print("ok  the approve endpoint is gone and 'APPROVE <id>' in chat is plain text")


def test_read_only_mcp_tools_still_work_for_their_owner():
    import mcp_server
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    with harness() as h:
        tok = AccessToken(token="redacted", client_id="pat:test", subject=f"user:{h.ids['A']}",
                          scopes=["portfolio:read"], expires_at=None)
        reset = auth_context_var.set(AuthenticatedUser(tok))
        try:
            out = asyncio.run(mcp_server.mcp.call_tool("get_holdings", {}))
            text = out[0][0].text if isinstance(out, tuple) else out[0].text
            assert "INFY-EQ" in text and "TCS-EQ" not in text, text
            summary = asyncio.run(mcp_server.mcp.call_tool("portfolio_summary", {}))
            assert summary
        finally:
            auth_context_var.reset(reset)
    print("ok  read-only MCP tools return the authenticated owner's data")


if __name__ == "__main__":
    run_all(dict(globals()))
