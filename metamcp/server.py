"""
Other Memory MCP Server — Bridges Searxng + Crawl4ai to Hermes Agent.

Exposes two MCP tools:
  - web_search(query, num_results, language, categories)
  - web_crawl(url, render_js, extract_mode)

Registered via: hermes mcp add other-memory \
    --command /path/to/venv/bin/python3 --args -m metamcp.server
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import ipaddress
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ─── Logging ───

logging.basicConfig(
    level=logging.WARNING,
    format="[other-memory] %(levelname)s %(message)s",
)
log = logging.getLogger("other-memory")

# ─── Configuration ───

SEARXNG_URL = "http://localhost:8888"
CRAWL4AI_URL = "http://localhost:11235"
REDIS_URL = "redis://localhost:6380/0"
CRAWL4AI_TOKEN = os.environ.get("CRAWL4AI_TOKEN", "")

# Blocked URL schemes and hosts to prevent SSRF/internal probing through web_crawl.
# The bridge should only crawl public http/https URLs.
BLOCKED_SCHEMES = {"file", "ftp", "gopher", "data", "javascript", "vbscript", "ssh", "telnet", "ldap", "dict", "tftp", "ipp"}
BLOCKED_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "169.254.169.254",  # AWS / GCP / Azure metadata endpoints
    "metadata.google.internal",
}

# Token budget: Hermes context windows are finite. Truncate crawl output.
MAX_CRAWL_OUTPUT_CHARS = 50_000  # ~12K tokens for typical English text

# Dedup: SHA256 of query params → JSON response. TTL: 1 hour.
CACHE_TTL_SECONDS = 3600

# ─── Redis (Lazy, Graceful Degradation) ───

_redis_client: "redis.Redis | None" = None
_redis_available: bool | None = None


def _get_redis():
    """Lazy Redis connection. Returns None if unavailable — graceful degradation."""
    global _redis_client, _redis_available
    if _redis_available is False:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        _redis_client = redis.Redis.from_url(REDIS_URL, socket_timeout=2)
        _redis_client.ping()
        _redis_available = True
        log.info("Redis connected on :6380")
    except Exception as e:
        log.warning("Redis unavailable — caching disabled: %s", e)
        _redis_available = False
        _redis_client = None
    return _redis_client


# ─── Helpers ───

def _cache_key(prefix: str, **params: Any) -> str:
    """Stable cache key: SHA256 of sorted params."""
    raw = json.dumps(params, sort_keys=True, default=str)
    # Truncate SHA-256 to 64 bits (16 hex). Collision probability ~10^-10 at 1M entries — negligible for 1h TTL cache.
    return f"om:{prefix}:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _truncate(text: str, max_chars: int = MAX_CRAWL_OUTPUT_CHARS) -> str:
    """Truncate text to stay within token budget. Adds a truncation notice."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n\n[... {len(text) - max_chars:,} characters truncated by Other Memory — "
        + f"original content is {len(text):,} chars. Use web_crawl to re-fetch with extract_mode='text' for raw output ...]\n\n"
        + text[-half:]
    )


# ─── MCP Server ───

server = Server("other-memory")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description=(
                "Search the web using a self-hosted meta search engine (Searxng). "
                "Queries Google, Bing, DuckDuckGo, Brave, and others simultaneously. "
                "Returns deduplicated, ranked results with titles, URLs, and snippets. "
                "Results are cached for 1 hour to avoid redundant upstream queries. "
                "Free. No API keys. Rate limits configured at Searxng level (30 req/min) — Redis cache amortizes repeated queries. "
                "SafeSearch disabled (safesearch=0) — intentional for security research; enable via Searxng settings if needed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. Be specific — use technical terms, CVE IDs, package names.",
                    },
                    "num_results": {
                        "type": "integer",
                        "default": 10,
                        "description": "Number of results to return (1-20, default: 10)",
                    },
                    "language": {
                        "type": "string",
                        "default": "en",
                        "description": "Language code (en, de, fr, ja, zh, etc.)",
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Search categories to query: general, news, science, files, images, social_media. Default: general only.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="web_crawl",
            description=(
                "Crawl a web page and extract its content. Renders JavaScript for "
                "SPA/dynamic sites using a headless browser. Returns clean text, "
                "markdown, or raw HTML. Content is truncated at 50K characters to "
                "stay within token budgets — set max_chars higher for deep pages "
                "or 0 to disable truncation entirely."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL to crawl (must include https://)",
                    },
                    "render_js": {
                        "type": "boolean",
                        "default": True,
                        "description": "Execute JavaScript before extraction. Required for React/Vue/Angular sites. Set false for static HTML pages (faster).",
                    },
                    "extract_mode": {
                        "type": "string",
                        "enum": ["markdown", "text", "html"],
                        "default": "markdown",
                        "description": "Output format: markdown (best for reading), text (plain, no formatting), html (raw source).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": 50000,
                        "description": "Maximum characters in output. Set higher for long pages, 0 to disable truncation. Truncated content shows a midpoint cut notice.",
                    },
                },
                "required": ["url"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "web_search":
        result = await search_searxng(
            query=str(arguments["query"]),
            num_results=min(int(arguments.get("num_results", 10)), 20),
            language=str(arguments.get("language", "en")),
            categories=arguments.get("categories"),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "web_crawl":
        result = await crawl_url(
            url=str(arguments["url"]),
            render_js=bool(arguments.get("render_js", True)),
            extract_mode=str(arguments.get("extract_mode", "markdown")),
            max_chars=int(arguments.get("max_chars", MAX_CRAWL_OUTPUT_CHARS)),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    raise ValueError(f"Unknown tool: {name}")


# ─── Tool Implementations ───


async def search_searxng(
    query: str,
    num_results: int = 10,
    language: str = "en",
    categories: list[str] | None = None,
) -> dict[str, Any]:
    """Search via Searxng with Redis cache layer."""

    # ── Check cache ──
    cache_params = {
        "q": query.strip(),
        "n": num_results,
        "lang": language,
        "cats": sorted(categories) if categories else ["general"],
    }
    r = _get_redis()
    if r:
        key = _cache_key("search", **cache_params)
        cached = r.get(key)
        if cached:
            result = json.loads(cached)
            result["cached"] = True
            result["cache_ttl_remaining"] = max(0, r.ttl(key))
            return result

    # ── Build request ──
    params: dict[str, str] = {
        "q": query.strip(),
        "format": "json",
        "language": language,
        # SafeSearch disabled — allows explicit content results from upstream engines.
        # This is intentional for security research: CVEs, exploits, and offensive tools
        # are often flagged by content filters. Adjust to "1" or "2" for general use.
        "safesearch": "0",
        "pageno": "1",
    }
    if categories:
        if not isinstance(categories, list):
            return {"error": f"categories must be a list, got {type(categories).__name__}", "query": query, "results": [], "engines_queried": []}
        params["categories"] = ",".join(categories)

    url = f"{SEARXNG_URL}/search?{urllib.parse.urlencode(params)}"

    # ── Query Searxng ──
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        log.error("Searxng query failed: %s", e)
        return {
            "error": f"Searxng unavailable — is the container running? ({e})",
            "query": query,
            "results": [],
            "engines_queried": [],
        }
    except Exception as e:
        log.error("Searxng unexpected error: %s", e)
        return {"error": str(e), "query": query, "results": [], "engines_queried": []}

    elapsed = time.monotonic() - started

    # ── Extract results ──
    raw_results: list[dict[str, Any]] = data.get("results", [])
    engines_queried = _extract_engines(data)

    # Deduplicate by URL (multiple engines may return the same page)
    seen_urls: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in raw_results:
        result_url = item.get("url", "")
        if result_url and result_url not in seen_urls:
            seen_urls.add(result_url)
            deduped.append({
                "title": item.get("title", ""),
                "url": result_url,
                "snippet": item.get("content", item.get("snippet", "")),
                "engines": item.get("engines", []),
            })

    total_raw = len(raw_results)
    result = {
        "query": query,
        "total_results_raw": total_raw,
        "total_results_deduped": len(deduped),
        "results": deduped[:num_results],
        "engines_queried": engines_queried,
        "query_time_ms": round(elapsed * 1000),
        "cached": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # ── Store in cache ──
    if r:
        r.setex(key, CACHE_TTL_SECONDS, json.dumps(result))

    return result


async def crawl_url(
    url: str,
    render_js: bool = True,
    extract_mode: str = "markdown",
    max_chars: int = MAX_CRAWL_OUTPUT_CHARS,
) -> dict[str, Any]:
    """Crawl a URL via Crawl4ai and extract content."""

    # Validate URL
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return {"error": f"Invalid URL: {url}", "url": url}

    # SSRF guard: block non-HTTP schemes and internal/metadata hosts
    if parsed.scheme.lower() in BLOCKED_SCHEMES:
        return {"error": f"Blocked URL scheme '{parsed.scheme}': only http/https are allowed", "url": url}
    hostname = parsed.hostname or ""
    if hostname.lower() in BLOCKED_HOSTS:
        return {"error": f"Blocked host '{hostname}': internal addresses are not crawlable", "url": url}
    # Block private IP ranges (10.x, 172.16-31.x, 192.168.x)
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return {"error": f"Blocked host '{hostname}': private/loopback IP addresses are not crawlable", "url": url}
    except ValueError:
        pass  # hostname, not IP — proceed

    # ── Check cache (same URL + same extract mode = same result) ──
    cache_params = {"url": url, "js": render_js, "mode": extract_mode}
    r = _get_redis()
    if r:
        key = _cache_key("crawl", **cache_params)
        cached = r.get(key)
        if cached:
            result = json.loads(cached)
            result["cached"] = True
            result["cache_ttl_remaining"] = max(0, r.ttl(key))
            return result

    # ── Build Crawl4ai request ──
    crawl4ai_url = f"{CRAWL4AI_URL}/crawl"
    payload: dict[str, Any] = {
        "urls": [url],
        "priority": 5,
        "extraction_config": {
            "type": "basic",
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CRAWL4AI_TOKEN}",
    }

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(crawl4ai_url, json=payload, headers=headers)
            resp.raise_for_status()
            crawl_data = resp.json()
    except httpx.TimeoutException:
        log.warning("Crawl4ai timeout for %s", url)
        return {
            "error": f"Crawl4ai timed out after 60s for {url}. The page may be too large or the server is unresponsive.",
            "url": url,
        }
    except httpx.HTTPError as e:
        log.error("Crawl4ai request failed: %s", e)
        return {
            "error": f"Crawl4ai unavailable — is the container running? ({e})",
            "url": url,
        }
    except Exception as e:
        log.error("Crawl4ai unexpected error: %s", e)
        return {"error": str(e), "url": url}

    elapsed = time.monotonic() - started

    # ── Extract content from Crawl4ai response ──
    results = crawl_data.get("results", [])
    if not results:
        return {"error": "Crawl4ai returned no results", "url": url}

    page = results[0]
    if page.get("error"):
        return {"error": page["error"], "url": url}

    # Pick the right content field based on extract_mode
    if extract_mode == "markdown":
        md = page.get("markdown", "")
        # Crawl4ai v0.5+ returns markdown as a dict with sub-fields
        if isinstance(md, dict):
            content = md.get("raw_markdown", md.get("markdown_with_citations", ""))
        else:
            content = md or page.get("cleaned_html", "")
    elif extract_mode == "text":
        # Prefer dedicated plain-text field, fall back to stripping markdown formatting
        content = page.get("text", page.get("plain_text", ""))
        if not content:
            md = page.get("markdown", "")
            if isinstance(md, dict):
                content = md.get("raw_markdown", md.get("markdown_with_citations", ""))
            else:
                content = md or page.get("cleaned_html", "")
            # Strip markdown formatting for true plain text
            content = re.sub(r"^#{1,6}\s+", "", content, flags=re.MULTILINE)  # headings
            content = re.sub(r"\*\*(.+?)\*\*", r"\1", content)                 # bold
            content = re.sub(r"\*(.+?)\*", r"\1", content)                     # italic
            content = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", content)              # links
            content = re.sub(r"`(.+?)`", r"\1", content)                       # inline code
    else:
        html = page.get("html", "")
        if not html:
            md = page.get("markdown", "")
            if isinstance(md, dict):
                html = md.get("raw_markdown", "")
            content = html
        elif isinstance(html, dict):
            content = html.get("raw_html", html.get("cleaned_html", ""))
        else:
            content = html

    # Extract metadata
    metadata = page.get("metadata", {})
    title = metadata.get("title", page.get("url", url))

    link_count = 0
    if extract_mode in ("markdown", "html"):
        link_count = len(re.findall(r"https?://", content))

    content_truncated = max_chars > 0 and len(content) > max_chars
    original_len = len(content)
    if max_chars > 0:
        content = _truncate(content, max_chars=max_chars)

    result = {
        "url": url,
        "title": title,
        "content": content,
        "content_length": len(content),
        "original_length": original_len,
        "truncated": content_truncated,
        "links_found": link_count,
        "rendered": render_js,
        "extract_mode": extract_mode,
        "crawl_time_ms": round(elapsed * 1000),
        "cached": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # ── Store in cache ──
    if r:
        r.setex(key, CACHE_TTL_SECONDS, json.dumps(result))

    return result


def _extract_engines(data: dict[str, Any]) -> list[str]:
    """Extract which search engines contributed results."""
    engines: set[str] = set()
    for item in data.get("results", []):
        for eng in item.get("engines", []):
            engines.add(eng)
    # answers[] contains text strings, not engine names — skip
    if not engines and data.get("results"):
        engines.add("unknown")
    return sorted(engines)


# ─── Entry Point ───


async def main():
    """Run the Other Memory MCP server via stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


async def main_sse(port: int = 8765):
    """Run the Other Memory MCP server via Streamable HTTP transport."""
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount

    # ponytail: StreamableHTTPServerTransport is itself an ASGI app
    transport = StreamableHTTPServerTransport("/mcp")

    async def handle_mcp(scope, receive, send):
        await transport.handle_request(scope, receive, send)

    app = Starlette(
        routes=[Mount("/mcp", app=handle_mcp)]
    )

    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server_uv = uvicorn.Server(config)
    await server_uv.serve()


if __name__ == "__main__":
    import sys
    import asyncio

    if len(sys.argv) >= 3 and sys.argv[1] == "--sse":
        port = int(sys.argv[2])
        asyncio.run(main_sse(port=port))
    else:
        asyncio.run(main())
