"""RAG Wrapper — lazy-imports rag_engine from the Tutor project.

Gracefully handles missing dependencies (sentence_transformers etc.) by
saving documents to the DB with status='pending' and skipping embeddings.
Re-process later via Tutor API /api/process/<doc_id> if needed.
"""

import json
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Lazy, defensible RAG engine import
# ---------------------------------------------------------------------------
_TUTOR_RAG_PATH = "/root/.openclaw/workspace/projects/oraculo/ai_oraculo_saas"
rag_engine = None
_rag_available = False
_rag_error = None

def _try_import():
    global rag_engine, _rag_available, _rag_error
    if rag_engine is not None:
        return  # already tried
    try:
        if _TUTOR_RAG_PATH not in sys.path:
            sys.path.insert(0, _TUTOR_RAG_PATH)
        import rag_engine as _re  # noqa: F811
        model = _re.get_model()
        if model is None:
            raise RuntimeError("Model not loaded")
        rag_engine = _re
        _rag_available = True
    except Exception as e:
        _rag_error = str(e)
        print(f"[RAG Wrapper] Engine unavailable: {_rag_error}")

def is_rag_available():
    _try_import()
    return _rag_available

# ---------------------------------------------------------------------------
# DB helpers (standalone, no RAG dependency)
# ---------------------------------------------------------------------------

DB_CFG = {"dbname": "ai_tutor_db", "user": "postgres", "host": "/var/run/postgresql"}

def _conn():
    import psycopg2
    return psycopg2.connect(**DB_CFG)


def create_document_in_tutor(name, area_id, content_text=None, url=None):
    """Insert a new document into the Tutor's documents table."""
    conn = _conn()
    try:
        cur = conn.cursor()
        is_external = url is not None
        cur.execute(
            """INSERT INTO documents (name, area_id, content_text, url, is_external_link, processing_status)
               VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING id""",
            (name, area_id, content_text, url, is_external),
        )
        doc_id = cur.fetchone()[0]
        conn.commit()
        return doc_id
    except Exception as e:
        conn.rollback()
        print(f"Error creating document in Tutor DB: {e}")
        return None
    finally:
        conn.close()


def update_document_content(doc_id, content_text):
    """Update the content_text of an existing Tutor document."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE documents SET content_text=%s, processing_status='pending' WHERE id=%s",
            (content_text, doc_id),
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"Error updating document {doc_id}: {e}")
        return False
    finally:
        conn.close()


def get_document(doc_id):
    """Fetch a Tutor document by ID."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, area_id, name, url, content_text, is_external_link, processing_status FROM documents WHERE id=%s",
            (doc_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {k: v for k, v in zip(
            ["id","area_id","name","url","content_text","is_external_link","processing_status"], row)}
    finally:
        conn.close()


def delete_old_chunks(doc_id):
    """Delete existing chunks for a document."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM document_chunks WHERE doc_id=%s", (doc_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"Error deleting chunks for doc {doc_id}: {e}")
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Full pipeline with graceful fallback
# ---------------------------------------------------------------------------

def process_document(doc_id):
    """Process a document through RAG. Returns result or fallback on failure."""
    _try_import()
    if not _rag_available:
        return {"ok": False, "chunks_created": 0, "saved_count": 0,
                "error": f"RAG engine unavailable ({_rag_error}). Doc saved with status=pending. Re-process via Tutor /api/process/{doc_id}"}
    try:
        if rag_engine is None:
            raise RuntimeError("engine not loaded")
        return rag_engine.process_document(doc_id)
    except Exception as e:
        print(f"RAG process error for doc {doc_id}: {e}")
        return {"ok": False, "chunks_created": 0, "saved_count": 0, "error": str(e)}


def ingest_and_index(content_text, name, area_id):
    """Create document + attempt RAG indexing. Never raises."""
    doc_id = create_document_in_tutor(name=name, area_id=area_id, content_text=content_text)
    if not doc_id:
        return {"ok": False, "error": "Failed to create document"}
    result = process_document(doc_id)
    return {"doc_id": doc_id, "name": name, **result}


def reprocess_existing(doc_id, new_content_text):
    """Update content + attempt RAG re-indexing. Never raises."""
    if not update_document_content(doc_id, new_content_text):
        return {"ok": False, "error": "Failed to update document"}
    delete_old_chunks(doc_id)
    result = process_document(doc_id)
    return {"doc_id": doc_id, **result}


def check_rag_engine_available():
    _try_import()
    return _rag_available
