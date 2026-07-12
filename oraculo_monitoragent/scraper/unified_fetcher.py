"""Unified content fetcher — fetches a monitored URL's own content plus any
PDF/TXT attachments it links to, and returns one combined text blob. Without
this, a change to an attached PDF (e.g. a government portal replacing a
form) is invisible to hash-based change detection, which today only looks
at the index page's own HTML text.

Entry point (fetch_unified_content) has the exact same Optional[str]
contract as the old scanner._fetch_content(): None means "fetch failed",
never raises.
"""

from typing import Optional
from urllib.parse import urlparse

from scraper.html_utils import classify_link_type
from scraper.http_fetcher import fetch_bytes, fetch_page as _fetch_page_http
from scraper.pdf_utils import extract_pdf_text

DEFAULT_MAX_ATTACHMENTS = 15
DEFAULT_MAX_ATTACHMENT_BYTES = 15_000_000
DEFAULT_ATTACHMENT_TIMEOUT = 15


def _extract_attachment_text(url: str, timeout: int, max_bytes: int) -> Optional[str]:
    """Download and extract text from a single pdf/txt/csv/xml attachment.
    None on any failure — callers skip silently, one bad attachment must
    never fail the whole scan."""
    try:
        data = fetch_bytes(url, timeout=timeout)
        if not data or len(data) > max_bytes:
            return None

        link_type = classify_link_type(url)
        if link_type == "pdf":
            return extract_pdf_text(data)

        # txt/csv/xml
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="ignore")
        import re
        text = re.sub(r"\s+", " ", text).strip()
        return text if len(text) > 50 else None
    except Exception as e:
        print(f"Attachment fetch error for {url}: {e}")
        return None


def _same_domain(url: str, root_netloc: str) -> bool:
    try:
        return urlparse(url).netloc == root_netloc
    except Exception:
        return False


def fetch_unified_content(
    url: str,
    fetch_mode: str = "http",
    timeout: int = 30,
    max_attachments: int = DEFAULT_MAX_ATTACHMENTS,
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    attachment_timeout: int = DEFAULT_ATTACHMENT_TIMEOUT,
) -> Optional[str]:
    """Fetch `url` and return its text unified with any same-domain PDF/TXT
    attachments it links to. If `url` itself is directly a pdf/txt/csv/xml
    file, fetches+extracts it directly without HTML parsing."""
    try:
        direct_type = classify_link_type(url)
        if direct_type in ("pdf", "txt"):
            return _extract_attachment_text(url, timeout=timeout, max_bytes=max_attachment_bytes)

        if fetch_mode == "js_browser":
            from scraper.js_renderer import fetch_page as _fetch_page_js
            page = _fetch_page_js(url, timeout=timeout)
        else:
            page = _fetch_page_http(url, timeout=timeout)

        if not page or not page.get("text"):
            return None

        main_text = page["text"]
        root_netloc = urlparse(url).netloc

        attachment_links = [
            link for link in page.get("links", [])
            if link.get("type") in ("pdf", "txt") and _same_domain(link["url"], root_netloc)
        ][:max_attachments]

        sections = [main_text]
        for link in attachment_links:
            att_text = _extract_attachment_text(
                link["url"], timeout=attachment_timeout, max_bytes=max_attachment_bytes
            )
            if att_text:
                sections.append(f"\n\n=== Anexo: {link.get('name', link['url'])} ({link['url']}) ===\n\n{att_text}")

        return "".join(sections)
    except Exception as e:
        print(f"Unified fetch error for {url} (mode={fetch_mode}): {e}")
        return None
