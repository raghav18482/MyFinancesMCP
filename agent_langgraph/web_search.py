"""
Tavily (preferred) or SerpAPI web search for market/news context.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


def search_web(query: str, max_results: int = 5, timeout: float = 15.0) -> str:
    """Return a short text summary of search results. Empty string if no provider configured."""
    query = (query or "").strip()
    if not query:
        return "Empty query."

    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        return _tavily_search(query, tavily_key, max_results, timeout)

    serp_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if serp_key:
        return _serpapi_search(query, serp_key, max_results, timeout)

    return (
        "Web search is not configured. Set TAVILY_API_KEY or SERPAPI_API_KEY in the server environment."
    )


def _tavily_search(query: str, api_key: str, max_results: int, timeout: float) -> str:
    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=api_key)
        resp: dict[str, Any] = client.search(query, max_results=max_results)
        results = resp.get("results") or []
        lines = [f"Tavily search: {query}", ""]
        for i, r in enumerate(results[:max_results], 1):
            title = r.get("title", "")
            url = r.get("url", "")
            content = (r.get("content") or "")[:500]
            lines.append(f"{i}. {title}")
            lines.append(f"   {url}")
            lines.append(f"   {content}")
            lines.append("")
        return "\n".join(lines) if len(lines) > 2 else "(No results.)"
    except Exception as e:
        logger.warning("Tavily search failed: %s", e)
        return f"Tavily error: {e}"


def _serpapi_search(query: str, api_key: str, max_results: int, timeout: float) -> str:
    try:
        r = requests.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google",
                "q": query,
                "api_key": api_key,
                "num": min(max_results, 10),
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        organic = data.get("organic_results") or []
        lines = [f"SerpAPI search: {query}", ""]
        for i, item in enumerate(organic[:max_results], 1):
            title = item.get("title", "")
            link = item.get("link", "")
            snippet = (item.get("snippet") or "")[:500]
            lines.append(f"{i}. {title}")
            lines.append(f"   {link}")
            lines.append(f"   {snippet}")
            lines.append("")
        return "\n".join(lines) if len(lines) > 2 else "(No results.)"
    except Exception as e:
        logger.warning("SerpAPI search failed: %s", e)
        return f"SerpAPI error: {e}"
