"""
Environment-driven settings for ADK agents.

OpenRouter via LiteLLM: set OPENROUTER_API_KEY (and optionally OPENROUTER_BASE_URL).
LiteLLM model id format: openrouter/<vendor>/<model>, e.g. openrouter/openai/gpt-4o-mini.

MCP bridge (optional): MCP_ACCOUNT_SERVER_URL — see integrations/mcp_account_bridge.py.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# Avoid compromised LiteLLM 1.82.7 / 1.82.8 (supply chain incident, March 2026).
_DEFAULT_LITELLM_MODEL = "openrouter/openai/gpt-4o-mini"


def adk_litellm_model() -> str:
    return os.environ.get("ADK_LITELLM_MODEL", _DEFAULT_LITELLM_MODEL).strip()


def adk_agent_temperature() -> float:
    return float(os.environ.get("ADK_AGENT_TEMPERATURE", "0.4"))


def openrouter_api_key() -> str:
    return (os.environ.get("OPENROUTER_API_KEY") or "").strip()
