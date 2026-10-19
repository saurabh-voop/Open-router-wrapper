"""Web search as an OpenRouter server tool (SPEC section 8.1).

OpenRouter runs the search; the model decides when to search. Beta feature: the
declaration below was checked against the OpenRouter docs source on 2026-10-19.
"""

from typing import Any

MAX_RESULTS = 5
MAX_TOTAL_RESULTS = 15


def web_search_tool() -> dict[str, Any]:
    return {
        "type": "openrouter:web_search",
        "parameters": {"max_results": MAX_RESULTS, "max_total_results": MAX_TOTAL_RESULTS},
    }


def citations_from_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Numbered, de-duplicated source links from ``url_citation`` annotations."""
    seen: dict[str, dict[str, Any]] = {}
    for ann in annotations:
        if ann.get("type") != "url_citation":
            continue
        cite = ann.get("url_citation") or {}
        url = cite.get("url")
        # Only web links: the URL comes from model/search output and is rendered as a link.
        if not isinstance(url, str) or not url.lower().startswith(("https://", "http://")):
            continue
        if url in seen:
            continue
        seen[url] = {"index": len(seen) + 1, "url": url, "title": cite.get("title") or url}
    return list(seen.values())
