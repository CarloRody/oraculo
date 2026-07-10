"""HTTP fetcher — downloads URLs and extracts clean text via BeautifulSoup."""

import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scraper.html_utils import extract_text as _extract_text_shared
from scraper.html_utils import extract_title as _extract_title_shared
from scraper.html_utils import parse_links as _parse_links_shared


def fetch_text(url: str, timeout: int = 30) -> Optional[str]:
    """Download a URL page and return clean body text.

    Returns None if the content is too short (<50 chars) or the request fails.
    """
    try:
        res = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Monitor-Agent/1.0"},
        )
        res.raise_for_status()

        soup = BeautifulSoup(res.text, "html.parser")

        # Remove non-content elements
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()

        return text if len(text) > 50 else None

    except Exception as e:
        print(f"Fetcher error for {url}: {e}")
        return None


def fetch_page(url: str, timeout: int = 30) -> Optional[dict]:
    """Download a URL once and return title, clean text, and links together —
    used by the recursive link crawler (same shape as js_renderer.fetch_page,
    so the crawler can dispatch by fetch_mode without caring which one ran).

    Returns None if the request fails."""
    try:
        res = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Monitor-Agent/1.0"},
        )
        res.raise_for_status()
        html = res.text
        return {
            "title": _extract_title_shared(html),
            "text": _extract_text_shared(html),
            "links": _parse_links_shared(html),
        }
    except Exception as e:
        print(f"fetch_page (http) error for {url}: {e}")
        return None


def fetch_bytes(url: str, timeout: int = 30) -> Optional[bytes]:
    """Download a URL and return raw bytes (for PDFs, etc.)."""
    try:
        res = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Monitor-Agent/1.0"},
        )
        res.raise_for_status()
        return res.content
    except Exception as e:
        print(f"Fetcher bytes error for {url}: {e}")
        return None
