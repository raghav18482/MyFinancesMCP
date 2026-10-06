"""News sentiment features, from FinBERT scores stored at ingest.

``services/sentiment_service.py`` runs a real transformer (ProsusAI/finbert)
and its output has never reached the price model — ``extract_features`` even
accepted a ``sentiment_scores`` argument and discarded it. This block closes
that loop.

Scores are read from a stored archive rather than computed on demand, for two
reasons: FinBERT on a few hundred thousand historical headlines is slow, and
re-scoring per call would make the same row produce different features on
different days, which is not reproducible.

Like the fundamental block, this is forward-only: the archive accumulates as
the app fetches news. Historical rows are null, and that is recorded rather
than papered over.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from services.features.contract import SENTIMENT

logger = logging.getLogger(__name__)

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NEWS_ROOT = os.path.join(_repo_root, "data", "pit", "news")

ARCHIVE_COLUMNS = ["symbol", "published_at", "title", "url", "label", "score"]

# FinBERT label -> signed score. Neutral is genuinely zero, not a small number.
_LABEL_SIGN = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}


def build(panel: pd.DataFrame, news_root: str | None = None) -> pd.DataFrame:
    """Add decayed sentiment aggregates over 1, 3 and 7 day windows."""
    if panel.empty:
        return panel.assign(**{f: np.nan for f in SENTIMENT})

    out = panel.copy()
    archive = read_archive(news_root or NEWS_ROOT)

    if archive.empty:
        # Every feature stays null, including sent_has_news. A zero there would
        # claim "we looked and found nothing", which is a different statement
        # from "we have no archive to look in" — and it would make the block
        # report itself as populated to the model registry.
        logger.info("sentiment: no scored-news archive yet — block will be null")
        for f in SENTIMENT:
            out[f] = np.nan
        return out

    daily = _daily_net(archive)
    out = out.merge(daily, on=["symbol", "date"], how="left")

    out["sent_net_1d"] = out["_net"].fillna(0.0)
    counts = out["_count"].fillna(0.0)

    for window, col in ((3, "sent_net_3d"), (7, "sent_net_7d")):
        out[col] = (
            out.groupby("symbol")["_net"]
            .transform(lambda s: s.fillna(0.0).rolling(window, min_periods=1).mean())
        )

    out["sent_article_count_7d"] = (
        counts.groupby(out["symbol"]).transform(
            lambda s: s.rolling(7, min_periods=1).sum()
        )
    )
    out["sent_has_news"] = (out["sent_article_count_7d"] > 0).astype(float)

    return out.drop(columns=["_net", "_count"], errors="ignore")


def _daily_net(archive: pd.DataFrame) -> pd.DataFrame:
    """Net signed sentiment and article count per symbol per day."""
    df = archive.copy()
    df["date"] = pd.to_datetime(df["published_at"]).dt.normalize()
    df["signed"] = df["label"].map(_LABEL_SIGN).fillna(0.0) * df["score"].fillna(0.0)

    grouped = (
        df.groupby(["symbol", "date"])
        .agg(_net=("signed", "mean"), _count=("signed", "size"))
        .reset_index()
    )
    return grouped


# ── Archive ────────────────────────────────────────────────────────────────
def write_articles(
    symbol: str,
    articles: list[dict],
    *,
    observed_on: Optional[date] = None,
    news_root: str = NEWS_ROOT,
) -> int:
    """Persist FinBERT-scored articles for one symbol.

    ``articles`` is the output of ``sentiment_service.analyze_articles`` — each
    entry carries a ``sentiment`` dict with ``label`` and ``confidence``.
    Deduplicated by URL within a day so a re-fetch does not double-count.
    """
    if not articles:
        return 0

    day = observed_on or date.today()
    rows = []
    for a in articles:
        sent = a.get("sentiment") or {}
        rows.append({
            "symbol": _norm(symbol),
            "published_at": pd.to_datetime(a.get("published_at") or a.get("published date")
                                           or day, errors="coerce"),
            "title": str(a.get("title") or "")[:300],
            "url": str(a.get("url") or a.get("link") or ""),
            "label": str(sent.get("label") or "neutral"),
            "score": float(sent.get("confidence") or 0.0),
        })

    new = pd.DataFrame(rows, columns=ARCHIVE_COLUMNS).dropna(subset=["published_at"])
    if new.empty:
        return 0

    path = os.path.join(news_root, f"date={day.isoformat()}", "part.parquet")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if os.path.exists(path):
        try:
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
        except Exception as e:
            logger.warning("sentiment: unreadable archive partition %s (%s)", path, e)

    new = new.drop_duplicates(subset=["symbol", "url", "title"], keep="last")
    new.to_parquet(path, index=False)
    return len(rows)


def read_archive(news_root: str = NEWS_ROOT) -> pd.DataFrame:
    if not os.path.isdir(news_root):
        return pd.DataFrame(columns=ARCHIVE_COLUMNS)

    frames = []
    for name in sorted(os.listdir(news_root)):
        path = os.path.join(news_root, name, "part.parquet")
        if os.path.exists(path):
            try:
                frames.append(pd.read_parquet(path))
            except Exception as e:
                logger.warning("sentiment: unreadable partition %s (%s)", name, e)

    if not frames:
        return pd.DataFrame(columns=ARCHIVE_COLUMNS)

    df = pd.concat(frames, ignore_index=True)
    df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce")
    return df.dropna(subset=["published_at"]).reset_index(drop=True)


def block_is_populated(panel: pd.DataFrame) -> bool:
    """Whether any row carries real news, for the registry record."""
    if "sent_has_news" not in panel.columns:
        return False
    return bool(pd.to_numeric(panel["sent_has_news"], errors="coerce").fillna(0).sum() > 0)


def coverage(news_root: str = NEWS_ROOT) -> dict:
    df = read_archive(news_root)
    if df.empty:
        return {"articles": 0, "symbols": 0, "first": None, "last": None}
    return {
        "articles": len(df),
        "symbols": int(df["symbol"].nunique()),
        "first": df["published_at"].min().date().isoformat(),
        "last": df["published_at"].max().date().isoformat(),
    }


def _norm(symbol: str) -> str:
    s = str(symbol).strip().upper()
    for suffix in ("-EQ", "-BE", ".NS"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s
