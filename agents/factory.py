"""
Generic LlmAgent factory: reuse for additional agents under agents/<name>/.
"""
from __future__ import annotations

import os
from typing import Any, Sequence

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.genai import types as genai_types

from agents.config import adk_agent_temperature, adk_litellm_model, openrouter_api_key


def build_openrouter_lite_llm(
    model: str | None = None,
    *,
    api_key: str | None = None,
) -> LiteLlm:
    """LiteLLM backend pointing at OpenRouter (OpenAI-compatible API)."""
    key = api_key if api_key is not None else openrouter_api_key()
    if not key:
        raise ValueError(
            "OPENROUTER_API_KEY is required for ADK agents using OpenRouter. "
            "Set it in the environment or pass api_key=."
        )
    mid = model or adk_litellm_model()
    # LiteLLM reads OPENROUTER_API_KEY from the environment for provider openrouter/*
    os.environ["OPENROUTER_API_KEY"] = key
    return LiteLlm(model=mid)


def build_llm_agent(
    *,
    name: str,
    instruction: str,
    tools: Sequence[Any],
    model: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
    description: str = "",
) -> LlmAgent:
    """Create an LlmAgent with OpenRouter (LiteLLM) and the given tool callables."""
    temp = temperature if temperature is not None else adk_agent_temperature()
    return LlmAgent(
        model=build_openrouter_lite_llm(model=model, api_key=api_key),
        name=name,
        instruction=instruction,
        tools=list(tools),
        description=description or name,
        generate_content_config=genai_types.GenerateContentConfig(temperature=temp),
    )
