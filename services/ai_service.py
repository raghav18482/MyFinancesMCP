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


# The research page is read by beginners who do not know what P/E or ROE mean,
# so this prompt trades analyst shorthand for plain language. The numbers, the
# score and the "ideal range" for each metric are all supplied in the payload —
# the model's job is to explain them, never to supply figures of its own.
FUNDAMENTAL_SYSTEM_PROMPT = """\
You explain a single company's fundamentals to someone who is new to investing \
and does not yet know what these ratios mean.

You receive JSON containing the company's metrics, a rule-based score computed \
from fixed thresholds, and a glossary giving the plain meaning and ideal range \
for each metric.

Rules:
- Write for a beginner. The first time you name a ratio, say what it measures in \
the same sentence ("its ROE of 21% — the return it earns on shareholders' money \
— is strong").
- Use ONLY the numbers in the JSON. Never estimate, recall, or infer a figure \
that is not there, and never mention the share price target or future returns.
- If something important is missing from the data, say so plainly rather than \
skipping over it.
- Judge each metric against the "ideal" text in the glossary, not against your \
own assumptions.
- If a "DO_NOT_JUDGE_THESE" block is present, obey it: those metrics are not \
meaningful for this kind of business and must not be called good or bad.
- A pillar marked "not scored" means there was no usable data for it. Say that \
plainly; do not invent an assessment for it.
- Do not tell the user to buy, sell, or hold.
- 180 words maximum. No markdown headings, no bullet lists — four short \
paragraphs of prose:
  1. What this company does, and what the score says overall.
  2. What it does well, with the two or three strongest numbers explained.
  3. What to watch, with the weakest numbers explained.
  4. One sentence on what the score cannot see (management, competition, \
litigation, what the future holds).
- End with exactly: This is not financial advice.
"""

FUNDAMENTAL_USER_TEMPLATE = """\
Explain this company's fundamentals to me as a beginner.

{fundamental_json}
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


def build_fundamental_prompt_payload(fundamentals: dict) -> dict:
    """Trim a fundamentals payload down to what the model needs.

    Drops the multi-year chart arrays and the per-band internals, and attaches
    the plain-English meaning plus ideal range for every metric that has a value,
    straight from ``data/metric_glossary.json``. That way the model grades
    against the same thresholds the score and the Learn page use instead of
    whatever it happens to remember about a ratio.
    """
    from services.fundamental_scoring import METRICS

    score = fundamentals.get("score") or {}
    valuation = fundamentals.get("valuation") or {}
    health = fundamentals.get("health") or {}
    excluded_keys = set(score.get("excluded") or [])

    def _label(key: str) -> str:
        return (METRICS.get(key) or {}).get("label", key)

    glossary: dict[str, dict] = {}
    for key, value in list(valuation.items()) + list(health.items()):
        meta = METRICS.get(key)
        if value is None or not meta or key in excluded_keys:
            continue
        glossary[key] = {
            "means": meta.get("plain", ""),
            "ideal": meta.get("ideal", ""),
        }

    graded: list[dict] = []
    missing: list[str] = []
    for pillar in score.get("pillars") or []:
        for row in pillar.get("metrics") or []:
            if row.get("verdict") == "unknown":
                missing.append(row.get("label"))
                continue
            if row.get("verdict") == "excluded":
                continue
            graded.append({
                "metric": row.get("label"),
                "value": row.get("display"),
                "verdict": row.get("verdict"),
                "assessment": row.get("note"),
            })

    # Metrics the sector override dropped are quarantined rather than listed
    # alongside the rest. Handing the model a bank's debt-to-equity of 812 in
    # the same block as its real metrics invites exactly the alarming,
    # wrong conclusion the override exists to prevent.
    quarantined = {
        _label(k): v
        for bucket in (valuation, health)
        for k, v in bucket.items()
        if k in excluded_keys and v is not None
    }

    def _visible(bucket: dict) -> dict:
        return {k: v for k, v in bucket.items() if v is not None and k not in excluded_keys}

    revenue_trend = fundamentals.get("revenue_trend") or []
    profit_trend = fundamentals.get("profit_trend") or []

    payload = {
        "company": {
            "name": fundamentals.get("company_name") or fundamentals.get("symbol"),
            "symbol": fundamentals.get("symbol"),
            "sector": fundamentals.get("sector"),
            "industry": fundamentals.get("industry"),
            "market_cap": fundamentals.get("market_cap"),
            "current_price": fundamentals.get("current_price"),
        },
        "score": {
            "out_of_100": score.get("total"),
            "grade": score.get("grade"),
            "label": score.get("label"),
            "confidence": score.get("confidence"),
            "metrics_with_data": f"{score.get('metrics_used')} of {score.get('metrics_total')}",
            "pillars": {
                p.get("label"): (
                    f"{p.get('score')} / {p.get('max')}" if p.get("score") is not None
                    else "not scored — no usable data for this company"
                )
                for p in (score.get("pillars") or [])
            },
        },
        "valuation": _visible(valuation),
        "financial_health": _visible(health),
        "graded_metrics": graded,
        "metric_glossary": glossary,
        "revenue_by_year": {p["year"]: p["value"] for p in revenue_trend if p.get("year")},
        "net_profit_by_year": {p["year"]: p["value"] for p in profit_trend if p.get("year")},
        "metrics_with_no_data": missing,
    }

    if quarantined:
        payload["DO_NOT_JUDGE_THESE"] = {
            "why": (
                f"These metrics are not meaningful for a {fundamentals.get('sector') or 'company'} "
                "business and were deliberately excluded from the score. They are shown on the "
                "page, so you may state the number if directly relevant, but you must NOT call "
                "them good or bad, treat them as strengths or concerns, or let them influence "
                "your overall read. A lender's high debt and negative cash flow are normal."
            ),
            "values": quarantined,
        }

    return payload


async def summarize_fundamentals(
    api_key: str,
    fundamentals: dict,
    model: str = DEFAULT_OPENROUTER_MODEL,
) -> str:
    """Explain one stock's fundamentals in plain language for a beginner."""
    if not api_key or not api_key.strip():
        raise ValueError("API key is required.")
    if not fundamentals or fundamentals.get("error"):
        raise ValueError(
            fundamentals.get("error") if fundamentals else "No fundamental data to summarise."
        )

    payload = build_fundamental_prompt_payload(fundamentals)
    fundamental_json = json.dumps(payload, indent=2, default=str)

    client = _make_client(api_key)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": FUNDAMENTAL_SYSTEM_PROMPT},
                {"role": "user", "content": FUNDAMENTAL_USER_TEMPLATE.format(
                    fundamental_json=fundamental_json,
                )},
            ],
            temperature=0.3,
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
