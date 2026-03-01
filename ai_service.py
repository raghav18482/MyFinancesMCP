import logging

from openai import AsyncOpenAI, AuthenticationError, APIError

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


async def generate_insights(
    api_key: str,
    portfolio_data: dict,
    model: str = "gpt-4o-mini",
) -> str:
    """Generate a structured portfolio insight from holdings data."""
    if not api_key or not api_key.strip():
        raise ValueError("API key is required.")

    import json
    portfolio_json = json.dumps(portfolio_data, indent=2, default=str)

    client = AsyncOpenAI(api_key=api_key.strip())
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
        raise ValueError("Invalid OpenAI API key. Please check and try again.")
    except APIError as e:
        logger.warning("OpenAI API error: %s", e)
        raise ValueError(f"OpenAI API error: {e.message}")


async def ask_question(
    api_key: str,
    question: str,
    portfolio_data: dict,
    model: str = "gpt-4o-mini",
) -> str:
    """Answer a user question with portfolio context."""
    if not api_key or not api_key.strip():
        raise ValueError("API key is required.")
    if not question or not question.strip():
        raise ValueError("Question cannot be empty.")

    import json
    portfolio_json = json.dumps(portfolio_data, indent=2, default=str)

    client = AsyncOpenAI(api_key=api_key.strip())
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
        raise ValueError("Invalid OpenAI API key. Please check and try again.")
    except APIError as e:
        logger.warning("OpenAI API error: %s", e)
        raise ValueError(f"OpenAI API error: {e.message}")
