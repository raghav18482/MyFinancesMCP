# LangGraph portfolio agent

## Overview

The dashboard **LangGraph Agent** (`/api/agent/langgraph/chat`) runs a ReAct-style loop with:

- **Broker tools** (in-process, same data as MCP): portfolio snapshot, LTP by symbol, funds
- **RAG** (ChromaDB + sentence-transformers): sector knowledge, product help, market basics
- **Web search** (optional): Tavily or SerpAPI
- **Long-term memory**: SQLite key-value preferences per user (`memory_get` / `memory_set`)
- **Short-term conversation**: LangGraph `InMemorySaver` checkpointer keyed by `thread_id` (process-local; restarts clear it)

## LLM provider (dashboard)

On the dashboard, choose **OpenAI** or **Google Gemini**, pick a **model**, then save your API key (browser-encrypted). API requests send `provider` (`openai` or `gemini`), `model`, and `api_key` in the JSON body.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Optional server-side fallback; UI usually passes the key in the request body |
| `GOOGLE_API_KEY` | Optional; same for Gemini (UI typically passes key in body) |
| `CHROMA_PATH` | Override Chroma persist dir (default: `./data/chroma`) |
| `ST_EMBEDDING_MODEL` | sentence-transformers model name (default: `all-MiniLM-L6-v2`) |
| `MEMORY_DB` | SQLite path for user preferences (default: `./data/agent_memory.db`) |
| `TAVILY_API_KEY` | Enables Tavily web search in tools |
| `SERPAPI_API_KEY` | Fallback web search if Tavily not set |
| `LANGGRAPH_RECURSION_LIMIT` | Max agent steps (default: `25`) |

## RAG setup

After install, build the vector index once (and after corpus changes):

```bash
python -m rag.ingest --rebuild
# or
python -m rag.ingest --rebuild --chroma-path /path/to/chroma
```

Corpus lives under `rag/corpus/` and `data/sector_cycles.json` / `data/sector_map.json`. Optional schemas for future collections: `rag/user_corpus_schema.json`.

## MCP parity

MCP tool **`get_agent_portfolio_json`** returns the same JSON shape as `build_portfolio_data()` used by the agent’s `get_portfolio_snapshot` tool. Shared module: `portfolio_snapshot.py`.

## `thread_id` behavior

- First request: omit `thread_id` or send empty; server returns a new UUID.
- Client should send the same `thread_id` on follow-up messages for conversation memory **within the same server process**.
- For a fresh conversation, clear `thread_id` (dashboard **New chat**).

## Production notes

- **InMemorySaver** does not survive multi-worker or process restarts. For durable threads use a supported LangGraph checkpointer (e.g. Postgres) — see LangGraph docs.
- Do not expose Tavily/SerpAPI keys to the browser; they belong only in server env.
- Chroma and `sentence-transformers` add image size and cold-start time; consider a Docker layer cache or lazy model load.
