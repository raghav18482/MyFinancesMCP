import json
import logging
import os

from openai import APIError, AsyncOpenAI, AuthenticationError

logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _openrouter_base_url() -> str:
    return os.environ.get("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL).rstrip("/")


def _openrouter_default_headers() -> dict[str, str] | None:
    headers: dict[str, str] = {}
    referer = os.environ.get("OPENROUTER_HTTP_REFERER")
    if referer:
        headers["HTTP-Referer"] = referer
    title = os.environ.get("OPENROUTER_APP_NAME")
    if title:
        headers["X-Title"] = title
    return headers or None


def _make_client(api_key: str) -> AsyncOpenAI:
    kwargs: dict = {
        "api_key": api_key.strip(),
        "base_url": _openrouter_base_url(),
    }
    dh = _openrouter_default_headers()
    if dh:
        kwargs["default_headers"] = dh
    return AsyncOpenAI(**kwargs)


SYSTEM_PROMPT = """\
You are a concise portfolio analyst. You will receive a JSON snapshot of the \
user's stock portfolio (holdings, P&L, funds). Analyse it and respond in \
plain text with clear section headers.

Rules:
- Be concise — 250 words max.
- Use rupee amounts (Rs.) formatted with commas.
- Never fabricate data that isn't in the snapshot.
- End with a one-line disclaimer: "This is not financial advice."
"""

INSIGHTS_USER_TEMPLATE = """\
Here is my portfolio snapshot:

{portfolio_json}

Give me a brief analysis covering:
1. Portfolio health (overall P&L, day trend)
2. Top 3 performers and bottom 3 laggards by P&L %
3. Concentration risk (any single holding > 20% of portfolio?)
4. 2-3 actionable suggestions
"""

QA_USER_TEMPLATE = """\
Here is my portfolio snapshot:

{portfolio_json}

My question: {question}

Answer based only on the data above. Be specific and concise.
"""


def _api_error_message(e: APIError) -> str:
    msg = getattr(e, "message", None) or str(e)
    return msg


async def generate_insights(
    api_key: str,
    portfolio_data: dict,
    model: str = DEFAULT_OPENROUTER_MODEL,
) -> str:
    """Generate a structured portfolio insight from holdings data."""
    if not api_key or not api_key.strip():
        raise ValueError("API key is required.")

    portfolio_json = json.dumps(portfolio_data, indent=2, default=str)

    client = _make_client(api_key)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": INSIGHTS_USER_TEMPLATE.format(
                    portfolio_json=portfolio_json,
                )},
            ],
            temperature=0.4,
            max_tokens=600,
        )
        return resp.choices[0].message.content or "No response generated."
    except AuthenticationError:
        raise ValueError(
            "Invalid OpenRouter API key. Please check and try again."
        ) from None
    except APIError as e:
        logger.warning("OpenRouter API error: %s", e)
        raise ValueError(f"OpenRouter API error: {_api_error_message(e)}") from None


async def ask_question(
    api_key: str,
    question: str,
    portfolio_data: dict,
    model: str = DEFAULT_OPENROUTER_MODEL,
) -> str:
    """Answer a user question with portfolio context."""
    if not api_key or not api_key.strip():
        raise ValueError("API key is required.")
    if not question or not question.strip():
        raise ValueError("Question cannot be empty.")

    portfolio_json = json.dumps(portfolio_data, indent=2, default=str)

    client = _make_client(api_key)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": QA_USER_TEMPLATE.format(
                    portfolio_json=portfolio_json,
                    question=question.strip(),
                )},
            ],
            temperature=0.4,
            max_tokens=500,
        )
        return resp.choices[0].message.content or "No response generated."
    except AuthenticationError:
        raise ValueError(
            "Invalid OpenRouter API key. Please check and try again."
        ) from None
    except APIError as e:
        logger.warning("OpenRouter API error: %s", e)
        raise ValueError(f"OpenRouter API error: {_api_error_message(e)}") from None
