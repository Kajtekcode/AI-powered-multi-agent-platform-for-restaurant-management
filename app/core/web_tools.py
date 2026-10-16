"""
Web tools for agents — internet research without API keys.

- web_search(query)  → DuckDuckGo HTML results (title, url, snippet)
- fetch_webpage(url) → readable text extract of any page

Used by the CEO/Growth agents for: competitor research, reviews,
local events, food trends, supplier prices.
"""

import logging
import re
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.S,
)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.S,
)


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#x27;|&apos;", "'", text)
    text = re.sub(r"&nbsp;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _decode_ddg_url(href: str) -> str:
    """DuckDuckGo wraps links: //duckduckgo.com/l/?uddg=<encoded>&..."""
    if "uddg=" in href:
        try:
            qs = urllib.parse.urlparse(href).query
            uddg = urllib.parse.parse_qs(qs).get("uddg", [""])[0]
            if uddg:
                return urllib.parse.unquote(uddg)
        except Exception:
            pass
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_ddg_html(html: str, max_results: int = 5) -> list:
    """Parse DuckDuckGo HTML results page (pure function — unit-testable)."""
    links = _RESULT_RE.findall(html)
    snippets = [_strip_tags(s) for s in _SNIPPET_RE.findall(html)]
    results = []
    seen = set()
    for i, (href, title) in enumerate(links):
        url = _decode_ddg_url(href)
        if url in seen or url.startswith("https://duckduckgo.com"):
            continue
        seen.add(url)
        results.append({
            "title": _strip_tags(title),
            "url": url,
            "snippet": snippets[i] if i < len(snippets) else "",
        })
        if len(results) >= max_results:
            break
    return results


def web_search(query: str, max_results: int = 5) -> dict:
    """Search the web via DuckDuckGo. No API key needed."""
    try:
        r = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": UA},
            timeout=15,
            follow_redirects=True,
        )
        r.raise_for_status()
        results = parse_ddg_html(r.text, max_results=max_results)
        if not results:
            return {"query": query, "results": [],
                    "note": "Brak wyników — spróbuj innego zapytania."}
        return {"query": query, "results": results}
    except Exception as e:
        logger.error(f"web_search failed: {e}")
        return {"query": query, "error": f"Wyszukiwanie nie powiodło się: {str(e)[:120]}"}


def fetch_webpage(url: str, max_chars: int = 5000) -> dict:
    """Fetch a URL and return readable text (scripts/styles stripped)."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
        r.raise_for_status()
        html = r.text
        # Drop non-content blocks
        html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
        html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
        html = re.sub(r"<nav.*?</nav>", " ", html, flags=re.S | re.I)
        html = re.sub(r"<footer.*?</footer>", " ", html, flags=re.S | re.I)
        text = _strip_tags(html)
        truncated = len(text) > max_chars
        return {
            "url": str(r.url),
            "text": text[:max_chars],
            "truncated": truncated,
        }
    except Exception as e:
        logger.error(f"fetch_webpage failed for {url}: {e}")
        return {"url": url, "error": f"Nie udało się pobrać strony: {str(e)[:120]}"}
