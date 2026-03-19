"""
ChromaDB persistent client and collection helpers.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_CHROMA = os.environ.get("CHROMA_PATH", "")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_chroma_path() -> str:
    if _DEFAULT_CHROMA:
        return _DEFAULT_CHROMA
    return str(_project_root() / "data" / "chroma")


class ChromaRAG:
    """Thin wrapper over Chroma collections with optional custom embedding function."""

    def __init__(self, persist_path: str | None = None):
        import chromadb
        from chromadb.config import Settings

        path = persist_path or default_chroma_path()
        Path(path).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=path,
            settings=Settings(anonymized_telemetry=False),
        )
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def collection_with_st(self, name: str):
        """Create collection that uses sentence-transformers via Chroma embedding_function API."""
        import chromadb.utils.embedding_functions as ef
        from rag.embeddings import ST_EMBEDDING_MODEL

        st_ef = ef.SentenceTransformerEmbeddingFunction(model_name=ST_EMBEDDING_MODEL)
        return self._client.get_or_create_collection(name=name, embedding_function=st_ef)

    def query(
        self,
        collection_name: str,
        query_text: str,
        n_results: int = 5,
        collection=None,
    ) -> list[dict[str, Any]]:
        """Return list of {text, metadata, distance}."""
        coll = collection
        if coll is None:
            coll = self.collection_with_st(collection_name)

        try:
            res = coll.query(query_texts=[query_text], n_results=n_results)
        except Exception as e:
            logger.warning("Chroma query error: %s", e)
            return []

        out = []
        ids = res.get("ids") or [[]]
        docs = res.get("documents") or [[]]
        metas = res.get("metadatas") or [[]]
        dists = res.get("distances") or [[]]
        row0 = 0
        for i in range(len(docs[row0] or [])):
            out.append({
                "text": docs[row0][i],
                "metadata": (metas[row0][i] if metas and metas[row0] else {}) or {},
                "distance": dists[row0][i] if dists and dists[row0] else None,
                "id": ids[row0][i] if ids and ids[row0] else None,
            })
        return out

    def ingest_documents(
        self,
        collection_name: str,
        documents: list[str],
        metadatas: list[dict] | None = None,
        ids: list[str] | None = None,
    ) -> None:
        coll = self.collection_with_st(collection_name)
        if ids is None:
            ids = [f"{collection_name}_{i}" for i in range(len(documents))]
        if metadatas is None:
            metadatas = [{}] * len(documents)
        # Upsert in batches for large corpora
        batch = 64
        for start in range(0, len(documents), batch):
            end = start + batch
            coll.upsert(
                ids=ids[start:end],
                documents=documents[start:end],
                metadatas=metadatas[start:end],
            )


def format_rag_hits(hits: list[dict]) -> str:
    """Human-readable block for LLM context."""
    if not hits:
        return "(No RAG results.)"
    lines = []
    for i, h in enumerate(hits, 1):
        meta = h.get("metadata") or {}
        src = meta.get("source", meta.get("type", "unknown"))
        lines.append(f"--- Chunk {i} (source: {src}) ---")
        lines.append(h.get("text", ""))
    return "\n".join(lines)


def get_default_rag() -> ChromaRAG:
    return ChromaRAG()


def multi_collection_query(
    rag: ChromaRAG,
    query_text: str,
    collections: list[str] | None = None,
    k_per_collection: int = 3,
) -> str:
    """Query several collections and merge top snippets."""
    names = collections or [
        "sector_knowledge",
        "product_help",
        "market_reference",
    ]
    all_hits: list[dict] = []
    for name in names:
        try:
            hits = rag.query(name, query_text, n_results=k_per_collection)
            for h in hits:
                h["_collection"] = name
            all_hits.extend(hits)
        except Exception as e:
            logger.debug("Skip collection %s: %s", name, e)
    all_hits.sort(key=lambda x: (x.get("distance") is None, x.get("distance") or 0))
    return format_rag_hits(all_hits[: max(5, k_per_collection * 2)])
