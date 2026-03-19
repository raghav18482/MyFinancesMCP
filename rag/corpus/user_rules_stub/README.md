# User rules RAG stub

Long-term investment preferences should be stored in SQLite `user_memory` (see agent memory tools) for exact key-value access.

Optional: mirror short natural-language summaries here and re-run `python -m rag.ingest --rebuild` to embed them into the `user_rules` Chroma collection for semantic recall.

Planned ETL (Phase 2b): export anonymized trade outcomes from Angel trade book into collection `user_trades_outcomes` with schema documented in `rag/user_corpus_schema.json`.
