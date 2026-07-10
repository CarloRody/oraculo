"""JS renderer — Playwright headless browser for JS-rendered portals.

Handles ASP.NET, SPA, and other portals that require JavaScript execution
to load content (e.g., nfe.fazenda.gov.br).
"""

from typing import Optional

from playwright.sync_api import sync_playwright

from scraper.html_utils import extract_text as _extract_text_shared
from scraper.html_utils import extract_title as _extract_title_shared
from scraper.html_utils import parse_links as _parse_links_shared

# Realistic Chrome user-agent to avoid bot detection / ASP.NET quirks
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _normalize_url(url: str) -> str:
    """Remove ASP.NET auto-detect cookie param that causes redirect loops."""
    if "AspxAutoDetectCookieSupport=" in url:
        import urllib.parse as urlparse
        parsed = urlparse.urlparse(url)
        qs = urlparse.parse_qs(parsed.query, keep_blank_values=True)
        qs.pop("AspxAutoDetectCookieSupport", None)
        new_query = urlparse.urlencode(qs, doseq=True)
        parsed = parsed._replace(query=new_query)
        url = urlparse.urlunparse(parsed)
    return url


def fetch_text(url: str, timeout: int = 60, wait_for_selector: str | None = None) -> Optional[str]:
    """Open a URL in headless Chromium and return clean body text.

    Returns None if content is too short (<50 chars) or the request fails.
    """
    try:
        url = _normalize_url(url)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()

            # domcontentloaded works better for ASP.NET portals than networkidle
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")

            if wait_for_selector:
                try:
                    page.wait_for_selector(wait_for_selector, timeout=15000)
                except Exception:
                    pass

            html = str(page.content())
            browser.close()

        return _extract_text_shared(html)

    except Exception as e:
        print(f"JS renderer error for {url}: {e}")
        return None


def extract_links(url: str, timeout: int = 60) -> list[dict]:
    """Navigate to a JS-rendered page and extract all downloadable links.

    Returns list of dicts with keys: name, url, type (pdf/html/txt).
    """
    links = []
    try:
        url = _normalize_url(url)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()

            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            html = str(page.content())
            browser.close()

        links = _parse_links_shared(html)

    except Exception as e:
        print(f"Link extraction error for {url}: {e}")

    return links


def fetch_page(url: str, timeout: int = 60) -> Optional[dict]:
    """Navigate to a JS-rendered page once and return title, clean text, and
    links together — used by the recursive link crawler, which needs both
    from every page without loading it twice.

    Returns None if navigation fails. Returns {"title", "text", "links"}
    otherwise (text may be None if the page has too little content).
    """
    try:
        url = _normalize_url(url)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()

            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            html = str(page.content())
            browser.close()

        return {
            "title": _extract_title_shared(html),
            "text": _extract_text_shared(html),
            "links": _parse_links_shared(html),
        }
    except Exception as e:
        print(f"fetch_page (js_browser) error for {url}: {e}")
        return None