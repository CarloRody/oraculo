"""PDF text extraction — mirrors ai_oraculo_saas/rag_engine.py's extract_pdf_text
so monitored PDFs can be diffed/versioned the same way HTML content is,
without importing rag_engine.py itself (that would pull in the Tutor's
torch/sentence-transformers deps — see rag_wrapper.py's docstring). pypdf is
pure-Python and lightweight, so it's added directly to this service's venv.
"""

import re
from io import BytesIO
from typing import Optional

from pypdf import PdfReader


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    """Extract clean text from PDF bytes. None if extraction fails or the
    result is too short (<50 chars) to be meaningful."""
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
        full_text = "\n".join(pages_text)
        full_text = re.sub(r"\s+", " ", full_text).strip()
        return full_text if len(full_text) > 50 else None
    except Exception as e:
        print(f"PDF extraction error: {e}")
        return None
