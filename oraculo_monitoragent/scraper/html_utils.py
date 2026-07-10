"""Shared HTML parsing helpers — no browser/network dependency.

Used by both http_fetcher.py (plain requests) and js_renderer.py (Playwright)
so the two fetch modes produce text/links/title the same way from raw HTML.
"""

import re
from typing import Optional

from bs4 import BeautifulSoup


def extract_text(html_str: str) -> Optional[str]:
    """Parse HTML and extract clean body text. None if too short (<50 chars)."""
    soup = BeautifulSoup(str(html_str), "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()

    return text if len(text) > 50 else None


def extract_title(html_str: str) -> Optional[str]:
    """Extract the <title> tag text, if present."""
    soup = BeautifulSoup(str(html_str), "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()[:500]
    return None


def parse_links(html_str: str) -> list[dict]:
    """Extract meaningful links from HTML. Returns list of dicts with keys: name, url, type (pdf/html/txt)."""
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

        try:
            clean_name = a_tag.get_text(strip=True)
        except Exception:
            clean_name = None
        if not clean_name:
            clean_name = "Unknown"
        else:
            clean_name = clean_name[:100]

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
