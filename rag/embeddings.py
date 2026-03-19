"""
Lazy singleton for sentence-transformers embeddings (local, no API cost).
"""
from __future__ import annotations

import logging
import os
from typing import Sequence

logger = logging.getLogger(__name__)

_model = None
ST_EMBEDDING_MODEL = os.environ.get("ST_EMBEDDING_MODEL", "all-MiniLM-L6-v2")


def get_embedding_model():
    """Load and cache the sentence-transformers model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading sentence-transformers model: %s", ST_EMBEDDING_MODEL)
        _model = SentenceTransformer(ST_EMBEDDING_MODEL)
    return _model


def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Encode a batch of strings to vectors."""
    if not texts:
        return []
    model = get_embedding_model()
    vectors = model.encode(list(texts), convert_to_numpy=True, show_progress_bar=False)
    return [v.tolist() for v in vectors]


def embed_query(text: str) -> list[float]:
    """Single query embedding."""
    return embed_texts([text])[0]
