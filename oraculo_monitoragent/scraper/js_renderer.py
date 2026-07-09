"""JS renderer — Playwright headless browser for JS-rendered portals.

Handles ASP.NET, SPA, and other portals that require JavaScript execution
to load content (e.g., nfe.fazenda.gov.br).
"""

import re
from typing import Optional

from playwright.sync_api import sync_playwright

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

        return _extract_text(html)

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

        links = _parse_links(html)

    except Exception as e:
        print(f"Link extraction error for {url}: {e}")

    return links


def _extract_text(html_str: str) -> Optional[str]:
    """Parse HTML and extract clean body text using BeautifulSoup."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(str(html_str), "html.parser")

    # Remove non-content elements
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()

    return text if len(text) > 50 else None


def _parse_links(html_str: str) -> list[dict]:
    """Extract meaningful links from HTML using BeautifulSoup."""
    from bs4 import BeautifulSoup

    links = []
    seen_urls = set()

    soup = BeautifulSoup(str(html_str), "html.parser")

    for a_tag in soup.find_all("a", href=True):
        raw_url = str(a_tag.get("href")).strip()

        if not raw_url:
            continue

        # Skip relative / internal / javascript URLs
        if not raw_url.startswith(("http://", "https://")):
            continue

        if raw_url in seen_urls:
            continue

        # Get visible text
        try:
            clean_name = a_tag.get_text(strip=True)
        except Exception:
            clean_name = None
        if not clean_name:
            clean_name = "Unknown"
        else:
            clean_name = clean_name[:100]

        # Determine type
        lower_url = raw_url.lower()
        if lower_url.endswith(".pdf"):
            link_type = "pdf"
        elif lower_url.endswith((".txt", ".csv", ".xml")):
            link_type = "txt"
        else:
            link_type = "html"

        links.append({
            "name": clean_name,
            "url": raw_url,
            "type": link_type,
        })
        seen_urls.add(raw_url)

    return links