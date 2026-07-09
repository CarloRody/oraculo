"""Content hasher — SHA256 computation and comparison for change detection."""

import hashlib


def content_hash(text: str) -> str:
    """Return SHA256 hex digest of the text content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compare(current_hash: str, stored_hash: str | None) -> tuple[str, bool]:
    """Compare current hash against stored hash.

    Returns (status, changed):
      - ('new', True)     — no stored hash yet (first scan)
      - ('changed', True)  — hash differs from stored
      - ('unchanged', False) — hash matches stored
    """
    if not stored_hash:
        return ("new", True)
    if current_hash != stored_hash:
        return ("changed", True)
    return ("unchanged", False)
