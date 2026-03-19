"""
Compile and run the LangGraph ReAct agent with shared checkpointer (in-memory).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from agent_langgraph.context import angel_client_var, user_key_var
from agent_langgraph.tools import all_tools
from llm_providers import GEMINI, default_model_for_provider, normalize_provider

logger = logging.getLogger(__name__)

_CHECKPOINTER = InMemorySaver()
_MAX_ITERATIONS = int(os.environ.get("LANGGRAPH_RECURSION_LIMIT", "25"))

AGENT_SYSTEM_PROMPT = """You are a concise portfolio assistant for Indian equity investors using Angel One.

Rules:
- For ANY question about the user's holdings, weights, P&L, cash, or orders, call get_portfolio_snapshot or other broker tools first. Never invent numbers.
- Use rag_search_financial_knowledge for sector context, metric definitions, and market basics — not for live prices.
- Use web_search_news for very recent headlines or macro news when tools alone are insufficient.
- Use memory_get / memory_set only for the user's stated preferences (risk, goals, sectors to avoid).
- Rupee amounts as Rs. with commas where helpful.
- End with: This is not financial advice.

If the user is not authenticated, say they must log in to the dashboard."""


def _build_chat_llm(*, llm_provider: str, api_key: str, model: str | None) -> BaseChatModel:
    prov = normalize_provider(llm_provider)
    mdl = (model or "").strip() or default_model_for_provider(prov)
    key = api_key.strip()
    if prov == GEMINI:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as e:
            raise ImportError(
                "Gemini support requires langchain-google-genai. "
                "Install dependencies: pip install langchain-google-genai google-generativeai"
            ) from e
        return ChatGoogleGenerativeAI(
            model=mdl,
            google_api_key=key,
            temperature=0.3,
        )
    return ChatOpenAI(
        model=mdl,
        api_key=key,
        temperature=0.3,
    )


async def run_langgraph_agent(
    *,
    client: Any,
    user_key: str,
    message: str,
    thread_id: str,
    api_key: str,
    model: str | None = None,
    llm_provider: str | None = None,
) -> str:
    """
    Run one agent turn. Conversation continuity uses checkpointer + thread_id.
    Context vars must be set for tools (done here).

    ``api_key`` is the OpenAI or Google AI (Gemini) key, depending on ``llm_provider``.
    """
    token = angel_client_var.set(client)
    uk_token = user_key_var.set(user_key or "anonymous")
    try:
        client.ensure_session()
        llm = _build_chat_llm(llm_provider=llm_provider or "", api_key=api_key, model=model)
        tools = all_tools()
        graph = create_react_agent(
            llm,
            tools,
            prompt=AGENT_SYSTEM_PROMPT,
            checkpointer=_CHECKPOINTER,
        )
        # Checkpointer merges by thread_id; send only the new user turn.
        messages = [HumanMessage(content=message)]
        config: dict[str, Any] = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": _MAX_ITERATIONS,
        }
        result = await graph.ainvoke({"messages": messages}, config)
        msgs = result.get("messages") or []
        if not msgs:
            return "No response from agent."
        last = msgs[-1]
        if isinstance(last, AIMessage):
            return (last.content or "").strip() or "No text response."
        return str(last.content)
    finally:
        angel_client_var.reset(token)
        user_key_var.reset(uk_token)


def resolve_user_key(client: Any, session_fallback: str | None) -> str:
    """Prefer Angel client code; else stable session id."""
    try:
        client.ensure_session()
        p = client.get_profile()
        if p.get("status") and p.get("data"):
            cc = p["data"].get("clientcode")
            if cc:
                return str(cc)
    except Exception as e:
        logger.debug("resolve_user_key profile: %s", e)
    if session_fallback:
        return f"session:{session_fallback}"
    return "anonymous"
