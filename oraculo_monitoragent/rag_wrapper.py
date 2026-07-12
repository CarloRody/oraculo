"""RAG Wrapper — delegates RAG processing to the Tutor's HTTP API.

The Tutor (ai_oraculo_saas) already runs on :5001 with its embedding model
warm in memory. Rather than importing rag_engine.py as a library — which
would require duplicating sentence-transformers/torch (~1.5GB) into this
service's own venv and loading a second copy of the model into RAM on a
3.8GB SBC — this wrapper calls the Tutor's existing
POST /api/process/<doc_id> endpoint over HTTP. Document rows are still
written directly to the shared Postgres DB (cheap, no heavy deps needed).
"""

import os

import requests

TUTOR_API_URL = os.environ.get("TUTOR_API_URL", "http://localhost:5001")

DB_CFG = {"dbname": "ai_tutor_db", "user": "postgres", "host": "/var/run/postgresql"}


def _conn():
    import psycopg2
    return psycopg2.connect(**DB_CFG)


def is_rag_available():
    """Check whether the Tutor's RAG engine (embedding model) is loaded and ready."""
    try:
        res = requests.get(f"{TUTOR_API_URL}/api/stats", timeout=5)
        res.raise_for_status()
        return res.json().get("rag_model") == "loaded"
    except Exception as e:
        print(f"[RAG Wrapper] Tutor API unavailable: {e}")
        return False


check_rag_engine_available = is_rag_available

# ---------------------------------------------------------------------------
# DB helpers (standalone, no RAG dependency)
# ---------------------------------------------------------------------------


def create_document_in_tutor(name, area_id, content_text=None, url=None, parent_doc_id=None, fetch_mode='http'):
    """Insert a new document into the Tutor's documents table.

    parent_doc_id links this document into a knowledge tree (Monitor Agent's
    recursive link crawl) — None means a root/standalone document, same as
    every document created before this feature existed."""
    conn = _conn()
    try:
        cur = conn.cursor()
        is_external = url is not None
        cur.execute(
            """INSERT INTO documents (name, area_id, content_text, url, is_external_link, processing_status, parent_doc_id, fetch_mode)
               VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id""",
            (name, area_id, content_text, url, is_external, parent_doc_id, fetch_mode),
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


def delete_document(doc_id):
    """Delete a Tutor document (and its chunks) via the Tutor's admin API —
    reuses the same endpoint the admin panel uses, instead of duplicating
    the cascading-delete SQL here. Used to roll back a cancelled crawl."""
    try:
        res = requests.delete(f"{TUTOR_API_URL}/admin/documents/{doc_id}", timeout=30)
        return res.status_code == 200
    except Exception as e:
        print(f"Error deleting document {doc_id}: {e}")
        return False


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


def find_existing_document(area_id, url):
    """id do documento mais recente já cadastrado pra essa (area_id, url), ou
    None — mesma query já usada em _process_rag_for_change (main.py), agora
    reaproveitada pelo crawl de árvore pra evitar duplicar a mesma URL toda
    vez que a árvore é recuperada de novo."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM documents WHERE area_id = %s AND url = %s ORDER BY upload_date DESC LIMIT 1",
            (area_id, url),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        print(f"Error finding existing document for area {area_id} url {url}: {e}")
        return None
    finally:
        conn.close()


def update_document_for_recrawl(doc_id, content_text, name, parent_doc_id, fetch_mode):
    """Atualiza um documento existente com o conteúdo/posição na árvore
    redescobertos numa nova recuperação de árvore — recrawl pode achar a
    página numa posição diferente (outro pai), então além do conteúdo
    também atualiza name/parent_doc_id/fetch_mode, não só content_text
    (diferente de update_document_content, usada pelo monitoramento de URL
    única onde a posição não muda)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE documents
               SET content_text=%s, name=%s, parent_doc_id=%s, fetch_mode=%s, processing_status='pending'
               WHERE id=%s""",
            (content_text, name, parent_doc_id, fetch_mode, doc_id),
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"Error updating document {doc_id} for recrawl: {e}")
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


# ---------------------------------------------------------------------------
# RAG processing via the Tutor's HTTP API
# ---------------------------------------------------------------------------

def process_document(doc_id):
    """Process a document through RAG by calling the Tutor's API. Never raises.

    The Tutor's /api/process/<doc_id> already clears old chunks before
    re-embedding, so no separate delete step is needed here.
    """
    try:
        res = requests.post(f"{TUTOR_API_URL}/api/process/{doc_id}", timeout=300)
        if res.status_code != 200:
            try:
                error = res.json().get("error", f"HTTP {res.status_code}")
            except Exception:
                error = f"HTTP {res.status_code}"
            return {"ok": False, "chunks_created": 0, "saved_count": 0, "error": error}
        data = res.json()
        return {
            "ok": True,
            "chunks_created": data.get("chunks_created", 0),
            "saved_count": data.get("saved_count", 0),
        }
    except Exception as e:
        print(f"RAG process error for doc {doc_id}: {e}")
        return {"ok": False, "chunks_created": 0, "saved_count": 0, "error": str(e)}


def ingest_and_index(content_text, name, area_id, url=None, parent_doc_id=None, fetch_mode='http'):
    """Create-or-update document + attempt RAG indexing. Never raises.

    Se já existe um documento pra essa (area_id, url), reaproveita e
    reprocessa em vez de criar uma linha nova — sem isso, toda vez que a
    árvore de links é recuperada de novo, as mesmas URLs viravam documentos
    duplicados e desatualizados (o antigo nunca era atualizado nem reusado)."""
    existing_id = find_existing_document(area_id, url) if url else None
    if existing_id:
        if not update_document_for_recrawl(existing_id, content_text, name, parent_doc_id, fetch_mode):
            return {"ok": False, "error": "Failed to update existing document"}
        result = process_document(existing_id)
        return {"doc_id": existing_id, "name": name, **result}

    doc_id = create_document_in_tutor(
        name=name, area_id=area_id, content_text=content_text,
        url=url, parent_doc_id=parent_doc_id, fetch_mode=fetch_mode
    )
    if not doc_id:
        return {"ok": False, "error": "Failed to create document"}
    result = process_document(doc_id)
    return {"doc_id": doc_id, "name": name, **result}


def reprocess_existing(doc_id, new_content_text):
    """Update content + attempt RAG re-indexing. Never raises."""
    if not update_document_content(doc_id, new_content_text):
        return {"ok": False, "error": "Failed to update document"}
    result = process_document(doc_id)
    return {"doc_id": doc_id, **result}


def create_pending_version(document_id, url_id, scan_id, content_text, content_hash):
    """Grava o conteúdo novo detectado numa mudança como uma versão 'pending'
    em vez de sobrescrever o documento na hora — só POST /versions/{id}/apply
    (main.py) de fato chama reprocess_existing(). Qualquer versão pending
    anterior do mesmo documento vira 'superseded': só a candidata mais
    recente é relevante pra revisão, comparar contra uma versão velha não
    ajuda em nada. Nunca levanta — quem chama trata None como falha."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE document_versions SET status='superseded' WHERE document_id=%s AND status='pending'",
            (document_id,),
        )
        cur.execute(
            """INSERT INTO document_versions (document_id, url_id, scan_id, content_text, content_hash, status)
               VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING id""",
            (document_id, url_id, scan_id, content_text, content_hash),
        )
        version_id = cur.fetchone()[0]
        conn.commit()
        return version_id
    except Exception as e:
        conn.rollback()
        print(f"Error creating pending version for document {document_id}: {e}")
        return None
    finally:
        conn.close()
