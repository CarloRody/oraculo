"""Crawler — helpers for the recursive link-tree feature (knowledge tree).

Each reviewed page becomes its own Tutor document (see rag_wrapper.py);
this module only handles fetching a page's {title, text, links} via the
right fetch mode, and the same-domain safety check. Depth/selection logic
itself lives in the /crawl/* routes in main.py.
"""

from typing import Optional
from urllib.parse import urlparse

# Hard ceilings, enforced server-side regardless of what the client sends.
MAX_DEPTH_CEILING = 5
MAX_PAGES_PER_CRAWL = 200


def fetch_page_for_crawl(url: str, fetch_mode: str = "http", timeout: Optional[int] = None) -> Optional[dict]:
    """Fetch a page's title/text/links using the appropriate method,
    mirroring the dispatch pattern in monitor/scanner.py."""
    if fetch_mode == "js_browser":
        from scraper.js_renderer import fetch_page as _fetch_js
        return _fetch_js(url, timeout=timeout or 60)
    else:
        from scraper.http_fetcher import fetch_page as _fetch_http
        return _fetch_http(url, timeout=timeout or 30)


def is_same_domain(url: str, root_netloc: str) -> bool:
    """True if url's host matches the root page's host exactly."""
    try:
        return urlparse(url).netloc == root_netloc
    except Exception:
        return False


def root_netloc(url: str) -> str:
    """Extract the netloc (host[:port]) of a URL, for same-domain comparisons."""
    return urlparse(url).netloc
