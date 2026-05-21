"""Web search and fetch skills."""
import logging
import os

import httpx

from engram.skills.decorator import skill

logger = logging.getLogger(__name__)


@skill(
    name="web_search",
    description="Search the web for current information. Returns a list of results with titles, URLs, and snippets.",
    parameters={
        "query": {"type": "string", "description": "Search query"},
        "num_results": {"type": "integer", "description": "Number of results to return", "default": 5},
    },
    required=["query"],
)
async def web_search(query: str, num_results: int = 5) -> dict:
    brave_key = os.environ.get("BRAVE_API_KEY")
    serper_key = os.environ.get("SERPER_API_KEY")

    if brave_key:
        return await _brave_search(query, num_results, brave_key)
    elif serper_key:
        return await _serper_search(query, num_results, serper_key)
    else:
        return {
            "results": [],
            "error": "No search API configured. Set BRAVE_API_KEY or SERPER_API_KEY.",
        }


async def _brave_search(query: str, num_results: int, api_key: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": num_results},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = [
            {"title": r["title"], "url": r["url"], "snippet": r.get("description", "")}
            for r in data.get("web", {}).get("results", [])
        ]
        return {"results": results, "query": query}


async def _serper_search(query: str, num_results: int, api_key: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": num_results},
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = [
            {"title": r["title"], "url": r["link"], "snippet": r.get("snippet", "")}
            for r in data.get("organic", [])
        ]
        return {"results": results, "query": query}


@skill(
    name="fetch_url",
    description="Fetch the text content of a URL. Useful for reading documentation, articles, or API responses.",
    parameters={
        "url": {"type": "string", "description": "URL to fetch"},
        "max_chars": {"type": "integer", "description": "Maximum characters to return", "default": 10000},
    },
    required=["url"],
)
async def fetch_url(url: str, max_chars: int = 10000) -> dict:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers={"User-Agent": "engram/0.1"})
            resp.raise_for_status()
            text = resp.text[:max_chars]
            return {"url": url, "content": text, "status_code": resp.status_code}
    except Exception as exc:
        return {"url": url, "error": str(exc), "content": ""}
