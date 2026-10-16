"""Pluggable web-search providers for the research engine.

Default is DuckDuckGo (free, no API key). Higher-quality providers (Brave,
Serper, Tavily) can be enabled by setting SEARCH_PROVIDER + SEARCH_API_KEY —
they return cleaner JSON and are far more reliable than scraping DuckDuckGo.

All providers return a uniform shape:
    [{"title": str, "url": str, "snippet": str}, ...]
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class SearchProvider(ABC):
    """A web-search backend. Implementations must be safe: never raise — return
    [] on failure so the research pipeline degrades gracefully."""

    name: str = "base"

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        ...


class DuckDuckGoProvider(SearchProvider):
    """Free, key-less. Scrapes the DuckDuckGo HTML endpoint. Reliable enough for
    a baseline; can be flaky/rate-limited under heavy use."""

    name = "duckduckgo"

    def search(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        from app.core.web_tools import parse_ddg_html
        try:
            r = httpx.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query}, headers={"User-Agent": UA},
                timeout=15, follow_redirects=True,
            )
            r.raise_for_status()
            return parse_ddg_html(r.text, max_results=max_results)
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed for '{query}': {e}")
            return []


class BraveProvider(SearchProvider):
    """Brave Search API — clean JSON, reliable. Needs SEARCH_API_KEY.
    https://brave.com/search/api/"""

    name = "brave"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        try:
            r = httpx.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max_results},
                headers={"X-Subscription-Token": self.api_key,
                         "Accept": "application/json"},
                timeout=15,
            )
            r.raise_for_status()
            web = (r.json().get("web") or {}).get("results", [])
            return [{"title": x.get("title", ""), "url": x.get("url", ""),
                     "snippet": x.get("description", "")} for x in web[:max_results]]
        except Exception as e:
            logger.warning(f"Brave search failed for '{query}': {e}")
            return []


class SerperProvider(SearchProvider):
    """Serper.dev (Google results as JSON). Needs SEARCH_API_KEY."""

    name = "serper"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        try:
            r = httpx.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                json={"q": query, "num": max_results}, timeout=15,
            )
            r.raise_for_status()
            organic = r.json().get("organic", [])
            return [{"title": x.get("title", ""), "url": x.get("link", ""),
                     "snippet": x.get("snippet", "")} for x in organic[:max_results]]
        except Exception as e:
            logger.warning(f"Serper search failed for '{query}': {e}")
            return []


class TavilyProvider(SearchProvider):
    """Tavily — search API tuned for LLM use. Needs SEARCH_API_KEY."""

    name = "tavily"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        try:
            r = httpx.post(
                "https://api.tavily.com/search",
                json={"api_key": self.api_key, "query": query,
                      "max_results": max_results}, timeout=20,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            return [{"title": x.get("title", ""), "url": x.get("url", ""),
                     "snippet": x.get("content", "")} for x in results[:max_results]]
        except Exception as e:
            logger.warning(f"Tavily search failed for '{query}': {e}")
            return []


def build_search_provider(provider: Optional[str] = None,
                          api_key: Optional[str] = None) -> SearchProvider:
    """Factory — reads settings if args not given. Falls back to DuckDuckGo
    when a paid provider is requested without a key."""
    from app.core.config import settings
    name = (provider or settings.search_provider or "duckduckgo").lower()
    key = api_key or settings.search_api_key

    if name in ("brave", "serper", "tavily") and not key:
        logger.warning(f"SEARCH_PROVIDER={name} but no SEARCH_API_KEY — falling back to DuckDuckGo")
        return DuckDuckGoProvider()

    if name == "brave":
        return BraveProvider(key)
    if name == "serper":
        return SerperProvider(key)
    if name == "tavily":
        return TavilyProvider(key)
    return DuckDuckGoProvider()
