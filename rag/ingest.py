"""
Build / rebuild Chroma collections from JSON data and Markdown corpus.
Run: python -m rag.ingest --rebuild
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

from rag.chroma_store import ChromaRAG, default_chroma_path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CORPUS = Path(__file__).resolve().parent / "corpus"


def _load_json(name: str) -> dict:
    path = DATA / name
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def chunk_sector_knowledge() -> tuple[list[str], list[dict], list[str]]:
    cycles = _load_json("sector_cycles.json")
    smap = _load_json("sector_map.json")
    docs: list[str] = []
    metas: list[dict] = []
    ids: list[str] = []

    for sector, note in cycles.items():
        text = f"Sector: {sector}\nDrivers and cycle notes:\n{note}"
        docs.append(text)
        metas.append({"source": "sector_cycles.json", "sector": sector, "type": "sector_knowledge"})
        ids.append(f"sector_cycle_{sector}")

    by_sector: dict[str, list[str]] = {}
    for sym, sec in smap.items():
        by_sector.setdefault(sec, []).append(sym)

    for sector, symbols in by_sector.items():
        syms = sorted(symbols)
        for i in range(0, len(syms), 40):
            batch = syms[i : i + 40]
            text = (
                f"Sector: {sector}\n"
                f"Sample symbols mapped to this sector (NSE-style symbols): {', '.join(batch)}"
            )
            docs.append(text)
            metas.append({
                "source": "sector_map.json",
                "sector": sector,
                "type": "sector_symbols",
            })
            ids.append(f"sector_map_{sector}_{i}")

    return docs, metas, ids


def chunk_markdown_dir(subdir: str, default_type: str) -> tuple[list[str], list[dict], list[str]]:
    base = CORPUS / subdir
    if not base.is_dir():
        return [], [], []
    docs, metas, ids = [], [], []
    for path in sorted(base.glob("**/*.md")):
        raw = path.read_text(encoding="utf-8")
        parts = re.split(r"\n##+\s+", raw)
        if len(parts) <= 1:
            chunks = [raw]
        else:
            chunks = [parts[0]] + [p.strip() for p in parts[1:] if p.strip()]
        rel = path.relative_to(base)
        for i, ch in enumerate(chunks):
            if len(ch.strip()) < 20:
                continue
            docs.append(ch.strip())
            metas.append({
                "source": str(rel),
                "type": default_type,
            })
            ids.append(f"{subdir}_{rel}_{i}".replace("/", "_"))
    return docs, metas, ids


def rebuild_all(persist_path: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    rag = ChromaRAG(persist_path=persist_path)
    logger.info("Chroma path: %s", rag.path)

    # sector_knowledge
    d, m, i = chunk_sector_knowledge()
    if d:
        rag.ingest_documents("sector_knowledge", d, m, i)
        logger.info("Ingested sector_knowledge: %d chunks", len(d))

    # product_help
    d, m, i = chunk_markdown_dir("product_help", "product_help")
    if d:
        rag.ingest_documents("product_help", d, m, i)
        logger.info("Ingested product_help: %d chunks", len(d))

    # market_reference
    d, m, i = chunk_markdown_dir("market_reference", "market_reference")
    if d:
        rag.ingest_documents("market_reference", d, m, i)
        logger.info("Ingested market_reference: %d chunks", len(d))

    # Optional empty collections created on first query — or ingest stubs
    d, m, i = chunk_markdown_dir("user_rules_stub", "user_rules")
    if d:
        rag.ingest_documents("user_rules", d, m, i)
        logger.info("Ingested user_rules stub: %d chunks", len(d))

    logger.info("Done. Default path: %s", default_chroma_path())


def main():
    p = argparse.ArgumentParser(description="Ingest RAG corpus into ChromaDB")
    p.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild collections from data/ and rag/corpus/",
    )
    p.add_argument("--chroma-path", default=None, help="Override CHROMA_PATH")
    args = p.parse_args()
    if args.rebuild:
        rebuild_all(persist_path=args.chroma_path)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
