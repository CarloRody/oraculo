"""Scanner — core logic: fetch → hash → compare → detect changes.

Supports two fetch modes:
- 'http': requests + BeautifulSoup (static HTML/PDF)
- 'js_browser': Playwright headless Chromium (JS-rendered portals)
"""

import time
from typing import Optional

from scraper.http_fetcher import fetch_text as _fetch_http
from monitor.hasher import content_hash, compare


def _fetch_content(url: str, fetch_mode: str = "http", timeout: int = 60) -> Optional[str]:
    """Fetch content using the appropriate method based on fetch_mode."""
    if fetch_mode == "js_browser":
        from scraper.js_renderer import fetch_text as _fetch_js
        return _fetch_js(url, timeout=timeout)
    else:
        return _fetch_http(url, timeout=timeout)


def scan_url(url_data: dict) -> dict:
    """Scan a single URL entry from the DB.

    Returns a scan result dict with status, hash, change info.
    """
    url = url_data["url"]
    url_id = url_data["id"]
    stored_hash = url_data.get("last_content_hash")

    start = time.time()

    # Fetch content using the configured mode
    fetch_mode = url_data.get("fetch_mode", "http")
    timeout = 60 if fetch_mode == "js_browser" else 30
    text = _fetch_content(url, fetch_mode=fetch_mode, timeout=timeout)

    duration = round(time.time() - start, 2)

    if text is None:
        return {
            "url_id": url_id,
            "status": "error",
            "change_type": "none",
            "content_hash": None,
            "docs_created": 0,
            "docs_updated": 0,
            "duration_seconds": duration,
            "error_message": f"Could not fetch content from {url} (mode={fetch_mode})",
        }

    current_hash = content_hash(text)
    status, changed = compare(current_hash, stored_hash)

    return {
        "url_id": url_id,
        "status": status,
        "change_type": "new_doc" if status == "new" else ("updated" if changed else "none"),
        "content_hash": current_hash,
        "docs_created": 1 if status == "new" else 0,
        "docs_updated": 1 if status == "changed" else 0,
        "duration_seconds": duration,
        "error_message": None,
        "_text": text,  # internal: content for RAG processing (stripped before DB)
    }


def scan_all(enabled_only: bool = True) -> list[dict]:
    """Scan all enabled URLs. Returns list of scan results."""
    from monitor.url_registry import list_urls

    urls = list_urls(enabled_only=enabled_only)
    results = []

    for url_data in urls:
        result = scan_url(url_data)
        results.append(result)

    return results
