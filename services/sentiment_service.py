"""
FinBERT-based sentiment analysis for portfolio news.
Uses ProsusAI/finbert for financial text classification (positive/negative/neutral).
"""
import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

LABELS = ["negative", "neutral", "positive"]
_model = None
_tokenizer = None
_cache: dict[str, dict[str, Any]] = {}


def _load_model() -> tuple[Any, Any]:
    """Lazy-load FinBERT model (first call takes ~5–10s, then cached in memory)."""
    global _model, _tokenizer
    if _model is None:
        try:
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            import torch
        except ImportError as e:
            raise ImportError(
                "FinBERT requires transformers and torch. "
                "Install with: pip install transformers torch"
            ) from e
        model_name = "ProsusAI/finbert"
        _tokenizer = AutoTokenizer.from_pretrained(model_name)
        _model = AutoModelForSequenceClassification.from_pretrained(model_name)
        _model.eval()
        logger.info("FinBERT model loaded successfully")
    return _model, _tokenizer


def analyze_text(text: str) -> dict[str, Any]:
    """
    Run FinBERT on a single text.
    Returns {"label": "positive"|"negative"|"neutral", "confidence": float, "scores": {...}}.
    """
    text = (text or "").strip()
    if not text:
        return {"label": "neutral", "confidence": 0.0, "scores": {"negative": 0.33, "neutral": 0.34, "positive": 0.33}}

    cache_key = hashlib.md5(text.encode("utf-8")).hexdigest()
    if cache_key in _cache:
        return _cache[cache_key]

    import torch
    model, tokenizer = _load_model()
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)

    with torch.no_grad():
        outputs = model(**inputs)

    probs = torch.nn.functional.softmax(outputs.logits, dim=-1)[0]
    scores = {LABELS[i]: round(float(probs[i]), 4) for i in range(3)}
    top_label = max(scores, key=scores.get)
    result = {
        "label": top_label,
        "confidence": scores[top_label],
        "scores": scores,
    }
    _cache[cache_key] = result
    return result


def analyze_articles(articles: list[dict]) -> list[dict]:
    """Analyze a batch of articles. Uses title + description as input text."""
    results = []
    for article in articles:
        title = article.get("title", "")
        desc = article.get("description", "")
        text = f"{title}. {desc}".strip() if desc else title
        sentiment = analyze_text(text)
        results.append({**article, "sentiment": sentiment})
    return results


def compute_sector_sentiment(articles_with_sentiment: list[dict]) -> dict[str, Any]:
    """Aggregate sentiment for a sector's articles."""
    if not articles_with_sentiment:
        return {
            "label": "neutral",
            "avg_score": 0.0,
            "bullish": 0,
            "bearish": 0,
            "neutral": 0,
        }

    pos_count = sum(1 for a in articles_with_sentiment if a.get("sentiment", {}).get("label") == "positive")
    neg_count = sum(1 for a in articles_with_sentiment if a.get("sentiment", {}).get("label") == "negative")
    neu_count = sum(1 for a in articles_with_sentiment if a.get("sentiment", {}).get("label") == "neutral")

    total = len(articles_with_sentiment)
    avg = (pos_count - neg_count) / total if total else 0.0

    if avg > 0.2:
        label = "bullish"
    elif avg < -0.2:
        label = "bearish"
    else:
        label = "neutral"

    return {
        "label": label,
        "avg_score": round(avg, 3),
        "bullish": pos_count,
        "bearish": neg_count,
        "neutral": neu_count,
    }
