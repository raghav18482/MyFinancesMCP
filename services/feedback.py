"""Landing-page feedback → AI reply → WhatsApp delivery.

Security model (see plan):
    The landing page is server-rendered and embeds a Fernet-signed token minted
    with ``ENCRYPTION_KEY`` (a server-only secret). The ``POST /api/feedback``
    endpoint requires that token back. Because the token is

      1. short-lived (Fernet ttl, see ``_TOKEN_TTL_SEC``),
      2. single-use (its nonce is consumed on first acceptance), and
      3. unforgeable without ``ENCRYPTION_KEY``,

    a curl copied from the browser's network tab stops working after one use or
    after it expires, and an attacker cannot mint fresh tokens on their own.
    Per-IP rate limiting is the practical cap against someone scripting GET / to
    harvest new tokens.
"""
from __future__ import annotations

import logging
import os
import re
import time
from uuid import uuid4

from openai import APIError, AuthenticationError

from db import sign_token, unsign_token
from services.ai_service import _make_client

logger = logging.getLogger(__name__)

# Direct OpenRouter slug (no ``openrouter/`` prefix — that's LiteLLM-only).
FEEDBACK_MODEL = os.environ.get(
    "FEEDBACK_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"
).strip()
PROJECT_URL = "https://myfinancesmcp.onrender.com/"
_CLOSING_LINE = f"Manage your portfolio smartly → {PROJECT_URL}"

_TOKEN_PURPOSE = "feedback"
_TOKEN_TTL_SEC = 600  # 10 minutes
_RATE_LIMIT_MAX = 3  # max accepted submissions per IP per window
_RATE_LIMIT_WINDOW_SEC = 600  # 10 minutes
_MAX_FEEDBACK_CHARS = 1000

# In-memory stores. Single-process friendly, mirroring session_manager.py.
_used_nonces: dict[str, float] = {}
_rate_hits: dict[str, list[float]] = {}

# NOTE on the model: nvidia/nemotron-* is a heavy reasoning model. Left unchecked
# it dumps its chain-of-thought ("We need to write a warm reply...") straight into
# `content` (no <think> tags), and any explicit character limit makes it literally
# count characters until it runs out of tokens. So we:
#   1. keep the prompt simple and conversational with NO character-count rule,
#   2. give few-shot examples so a weak model just imitates the output shape,
#   3. sanitise the output: if it still rambles, pull the quoted reply it drafted
#      mid-thought, and otherwise use a clean static fallback.
_SYSTEM_PROMPT = (
    "You are a member of the MyFinanceMCP team replying personally to someone who "
    "just left feedback on our website. Your reply is sent straight to them on "
    "WhatsApp, so it has to read like a real, warm message from a human.\n\n"
    "MyFinanceMCP is an AI-powered Angel One portfolio tracker: live holdings, "
    "positions and P&L, AI portfolio insights, daily WhatsApp briefings, and MCP "
    "support for Claude, ChatGPT and Cursor.\n\n"
    "Always: speak directly to the person (\"you\"), thank them, react to what they "
    "actually said, and warmly invite them to keep using MyFinanceMCP. If they ask "
    "for something we don't have yet, tell them we've noted it / are looking into "
    "it. Never claim a feature exists when it doesn't.\n\n"
    "Reply with one or two short, friendly sentences and nothing else. Do not "
    "explain your thinking, do not write notes or labels, and do not use markdown, "
    "bullet points, links, or emojis. Just write the message."
)

# Few-shot examples anchor the output shape so the model writes the message
# directly instead of reasoning out loud.
_FEWSHOT: list[dict[str, str]] = [
    {
        "role": "user",
        "content": (
            "Feedback from a visitor named Priya:\n"
            "\"The dashboard looks clean but I'd love a sector-wise breakdown.\""
        ),
    },
    {
        "role": "assistant",
        "content": (
            "Thanks so much, Priya! Really glad the dashboard feels clean for you, "
            "and a sector-wise breakdown is a great idea that's on our radar. We'd "
            "love for you to keep tracking your portfolio with MyFinanceMCP."
        ),
    },
    {
        "role": "user",
        "content": (
            "Feedback from a visitor:\n"
            "\"Love the daily WhatsApp briefings, they actually help.\""
        ),
    },
    {
        "role": "assistant",
        "content": (
            "Thank you, that genuinely makes our day to hear! The daily briefings "
            "are one of our favourite parts too, and there's plenty more we're "
            "building. Enjoy MyFinanceMCP!"
        ),
    },
]

# Phrases that only appear when the model leaks its planning/reasoning instead of
# writing a real customer message. If any show up, we try to recover a drafted
# reply, then fall back to the static message.
_LEAK_MARKERS = (
    "we need to",
    "we should mention",
    "we should say",
    "must be plain text",
    "no markdown",
    "no bullet",
    "no emojis",
    "no links",
    "under 350",
    "under 300",
    "characters",
    "gently promote",
    "let me ",
    "i should ",
    "i need to ",
    "something like:",
)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_DANGLING_THINK_RE = re.compile(r"<think>.*$", re.IGNORECASE | re.DOTALL)
# Drafted replies appear inside straight or curly quotes; grab sentence-like ones.
_QUOTED_RE = re.compile(r"[\"\u201c\u201d]([^\"\u201c\u201d]{40,400})[\"\u201c\u201d]")


class FeedbackError(Exception):
    """Recoverable feedback failure with a user-safe message and HTTP status."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _prune_nonces(now: float) -> None:
    cutoff = now - _TOKEN_TTL_SEC
    stale = [n for n, ts in _used_nonces.items() if ts < cutoff]
    for n in stale:
        _used_nonces.pop(n, None)


def issue_feedback_token() -> str:
    """Mint a single-use, time-limited token to embed in the landing page."""
    return sign_token({"purpose": _TOKEN_PURPOSE, "nonce": uuid4().hex})


def consume_feedback_token(token: str) -> None:
    """Validate and consume a feedback token. Raises :class:`FeedbackError`."""
    try:
        payload = unsign_token(token, max_age=_TOKEN_TTL_SEC)
    except Exception:
        raise FeedbackError("This form expired. Please refresh and try again.", 403)

    if payload.get("purpose") != _TOKEN_PURPOSE:
        raise FeedbackError("Invalid request.", 403)

    nonce = payload.get("nonce")
    if not nonce or not isinstance(nonce, str):
        raise FeedbackError("Invalid request.", 403)

    now = time.time()
    _prune_nonces(now)
    if nonce in _used_nonces:
        raise FeedbackError("This form was already submitted. Please refresh.", 403)
    _used_nonces[nonce] = now


def check_rate_limit(client_ip: str) -> None:
    """Enforce a per-IP submission cap. Raises :class:`FeedbackError` (429)."""
    now = time.time()
    hits = [t for t in _rate_hits.get(client_ip, []) if now - t < _RATE_LIMIT_WINDOW_SEC]
    if len(hits) >= _RATE_LIMIT_MAX:
        raise FeedbackError("Too many submissions. Please try again later.", 429)
    hits.append(now)
    _rate_hits[client_ip] = hits


def _static_reply(name: str | None) -> str:
    who = f" {name.strip()}" if name and name.strip() else ""
    return (
        f"Thanks{who}! Your feedback genuinely means a lot to us and helps shape "
        "what we build next on MyFinanceMCP. We'd love for you to keep tracking "
        "your portfolio with us."
    )


def _trim_to_limit(text: str, limit: int = 320) -> str:
    """Trim to ``limit`` chars, preferring to end on a full sentence."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= 60:
        return cut[: end + 1].rstrip()
    return cut.rstrip().rstrip(",;:") + "..."


def _looks_like_reasoning(text: str) -> bool:
    return any(marker in text.lower() for marker in _LEAK_MARKERS)


def _recover_quoted_draft(text: str) -> str | None:
    """When the model thinks out loud, it usually drafts the reply in quotes.
    Return the last clean quoted sentence that isn't itself reasoning."""
    for candidate in reversed(_QUOTED_RE.findall(text)):
        c = candidate.strip()
        if c and c[0].isupper() and not _looks_like_reasoning(c):
            return c
    return None


def _sanitize_reply(text: str | None) -> str | None:
    """Strip reasoning/markup leakage. Returns ``None`` if no usable reply remains
    (so the caller uses the static fallback)."""
    if not text:
        return None
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _DANGLING_THINK_RE.sub("", cleaned)  # truncated, unclosed <think>
    cleaned = cleaned.strip().strip("\"'\u201c\u201d").strip()
    if not cleaned:
        return None
    if _looks_like_reasoning(cleaned):
        # The model rambled — try to recover the reply it drafted in quotes.
        recovered = _recover_quoted_draft(text)
        return _trim_to_limit(recovered) if recovered else None
    return _trim_to_limit(cleaned)


async def generate_reply(feedback: str, name: str | None = None) -> str:
    """Generate a short, human reply to the feedback, appending the project link.

    Uses a reasoning-aware prompt and post-processing so the chain-of-thought of
    the free Nemotron model never leaks into the message. Falls back to a clean
    static thank-you on any failure, so the user always receives a real message.
    """
    body = _static_reply(name)
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if api_key:
        who = f" named {name.strip()}" if name and name.strip() else ""
        user_msg = f"Feedback from a visitor{who}:\n\"{feedback.strip()}\""
        try:
            client = _make_client(api_key)
            resp = await client.chat.completions.create(
                model=FEEDBACK_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    *_FEWSHOT,
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.6,
                max_tokens=500,
                extra_body={"reasoning": {"exclude": True}},
            )
            ai_text = _sanitize_reply(resp.choices[0].message.content)
            if ai_text:
                body = ai_text
            else:
                logger.warning("[feedback] AI reply unusable (leak/empty), using fallback")
        except (AuthenticationError, APIError) as e:
            logger.warning("[feedback] AI reply failed, using fallback: %s", e)
        except Exception:
            logger.exception("[feedback] unexpected AI error, using fallback")
    else:
        logger.warning("[feedback] OPENROUTER_API_KEY missing, using static reply")

    return f"{body}\n\n{_CLOSING_LINE}"
