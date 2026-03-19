"""
Shared LLM provider ids and defaults for dashboard AI (OpenAI + Google Gemini).
"""
from __future__ import annotations

# Normalized provider strings from API / UI
OPENAI = "openai"
GEMINI = "gemini"

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"


def normalize_provider(raw: str | None) -> str:
    p = (raw or OPENAI).strip().lower()
    if p in ("google", "google_gemini", "google-genai"):
        return GEMINI
    if p == GEMINI:
        return GEMINI
    return OPENAI


def default_model_for_provider(provider: str) -> str:
    return DEFAULT_GEMINI_MODEL if provider == GEMINI else DEFAULT_OPENAI_MODEL
