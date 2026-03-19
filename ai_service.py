import logging
from typing import Optional

from openai import APIError, AsyncOpenAI, AuthenticationError

from llm_providers import GEMINI, default_model_for_provider, normalize_provider

logger = logging.getLogger(__name__)

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


async def _openai_chat(
    api_key: str,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    temperature: float,
) -> str:
    client = AsyncOpenAI(api_key=api_key.strip())
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or "No response generated."
    except AuthenticationError:
        raise ValueError("Invalid OpenAI API key. Please check and try again.")
    except APIError as e:
        logger.warning("OpenAI API error: %s", e)
        raise ValueError(f"OpenAI API error: {e.message}")


async def _gemini_chat(
    api_key: str,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    temperature: float,
) -> str:
    import google.generativeai as genai
    from google.api_core import exceptions as google_exceptions

    genai.configure(api_key=api_key.strip())
    try:
        m = genai.GenerativeModel(
            model_name=model,
            system_instruction=system,
        )
        resp = await m.generate_content_async(
            user,
            generation_config=genai.types.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
    except google_exceptions.PermissionDenied as e:
        raise ValueError("Invalid or unauthorized Google API key. Please check and try again.") from e
    except google_exceptions.InvalidArgument as e:
        raise ValueError(f"Gemini request error: {e}") from e
    except Exception as e:
        logger.warning("Gemini API error: %s", e)
        msg = str(e) or type(e).__name__
        raise ValueError(f"Gemini API error: {msg}") from e

    if not resp.candidates:
        return "No response generated."
    try:
        text = resp.text
    except ValueError:
        # Blocked or empty finish reason
        return "No response generated."
    return (text or "").strip() or "No response generated."


async def generate_insights(
    api_key: str,
    portfolio_data: dict,
    model: Optional[str] = None,
    *,
    provider: Optional[str] = None,
) -> str:
    """Generate a structured portfolio insight from holdings data."""
    if not api_key or not api_key.strip():
        raise ValueError("API key is required.")

    import json

    prov = normalize_provider(provider)
    mdl = (model or "").strip() or default_model_for_provider(prov)
    portfolio_json = json.dumps(portfolio_data, indent=2, default=str)
    user_content = INSIGHTS_USER_TEMPLATE.format(portfolio_json=portfolio_json)

    if prov == GEMINI:
        return await _gemini_chat(
            api_key,
            mdl,
            SYSTEM_PROMPT,
            user_content,
            max_tokens=600,
            temperature=0.4,
        )
    return await _openai_chat(
        api_key,
        mdl,
        SYSTEM_PROMPT,
        user_content,
        max_tokens=600,
        temperature=0.4,
    )


async def ask_question(
    api_key: str,
    question: str,
    portfolio_data: dict,
    model: Optional[str] = None,
    *,
    provider: Optional[str] = None,
) -> str:
    """Answer a user question with portfolio context."""
    if not api_key or not api_key.strip():
        raise ValueError("API key is required.")
    if not question or not question.strip():
        raise ValueError("Question cannot be empty.")

    import json

    prov = normalize_provider(provider)
    mdl = (model or "").strip() or default_model_for_provider(prov)
    portfolio_json = json.dumps(portfolio_data, indent=2, default=str)
    user_content = QA_USER_TEMPLATE.format(
        portfolio_json=portfolio_json,
        question=question.strip(),
    )

    if prov == GEMINI:
        return await _gemini_chat(
            api_key,
            mdl,
            SYSTEM_PROMPT,
            user_content,
            max_tokens=500,
            temperature=0.4,
        )
    return await _openai_chat(
        api_key,
        mdl,
        SYSTEM_PROMPT,
        user_content,
        max_tokens=500,
        temperature=0.4,
    )
