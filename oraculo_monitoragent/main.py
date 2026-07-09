"""Monitor Agent API — FastAPI application (port :5003)."""

from pathlib import Path
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


config = load_config()

# ---------------------------------------------------------------------------
# DB helper (sync, lightweight — no ORM)
# ---------------------------------------------------------------------------

import psycopg2
from psycopg2.extras import RealDictCursor


def get_db():
    db_cfg = config["database"]
    conn = psycopg2.connect(**db_cfg)
    conn.cursor().execute("SET statement_timeout = '60s'")
    return conn


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: run migrations if needed
    from db_migrations import migrate_if_needed
    migrate_if_needed()
    yield
    # shutdown: nothing to clean up


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Monitor Agent API",
    description="Independent monitoring service for AI Tutor external URLs. Zero changes to the Tutor itself.",
    version="0.1.0",
    lifespan=lifespan,
)

# Serve static frontend
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
STATIC_DIR = Path(__file__).parent / "public"

@app.get("/")
def serve_frontend():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "Frontend not found. Create public/index.html"}

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class URLCreate(BaseModel):
    name: str = Field(..., max_length=255)
    url: str
    area_id: int | None = None
    fetch_mode: str = "http"  # 'http' or 'js_browser'
    enabled: bool = True


class URLUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    area_id: int | None = None
    fetch_mode: str | None = None
    enabled: bool | None = None


class ScanResult(BaseModel):
    status: str  # 'unchanged', 'changed', 'error'
    docs_created: int = 0
    docs_updated: int = 0
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Routes — Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "service": "monitor-agent-api",
        "version": "0.1.0",
        "port": config["server"]["port"],
        "db_connected": db_ok,
    }


# ---------------------------------------------------------------------------
# Routes — URLs CRUD
# ---------------------------------------------------------------------------

@app.get("/urls")
def list_urls(enabled_only: bool = True):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if enabled_only:
            cur.execute("SELECT * FROM monitor_urls WHERE enabled ORDER BY name")
        else:
            cur.execute("SELECT * FROM monitor_urls ORDER BY name")
        rows = cur.fetchall()
        return {"urls": [dict(r) for r in rows], "total": len(rows)}
    finally:
        conn.close()


@app.post("/urls", status_code=201)
def create_url(url_data: URLCreate):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """INSERT INTO monitor_urls (name, url, area_id, fetch_mode, enabled)
               VALUES (%s, %s, %s, %s, %s)
               RETURNING *""",
            (url_data.name, url_data.url, url_data.area_id, url_data.fetch_mode, url_data.enabled),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(409, f"URL already registered: {url_data.url}")
    finally:
        conn.close()


@app.patch("/urls/{url_id}")
def update_url(url_id: int, url_data: URLUpdate):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Build dynamic UPDATE from provided fields
        updates = []
        params = []
        data_dict = url_data.model_dump(exclude_none=True)
        for key, value in data_dict.items():
            updates.append(f"{key} = %s")
            params.append(value)

        if not updates:
            raise HTTPException(400, "No fields to update")

        params.append(url_id)
        sql = f"UPDATE monitor_urls SET {', '.join(updates)} WHERE id = %s RETURNING *"
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "URL not found")
        conn.commit()
        return dict(row)
    finally:
        conn.close()


@app.delete("/urls/{url_id}")
def delete_url(url_id: int):
    """Stop monitoring a URL. Only removes monitor_urls (cascades to monitor_scans
    and monitor_extracted_links). Never touches the Tutor's documents/document_chunks —
    the indexed document and its RAG chunks stay intact and can only be deleted
    from the Tutor's admin panel."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM monitor_urls WHERE id = %s RETURNING id", (url_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "URL not found")
        conn.commit()
        return {"deleted": True, "id": url_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes — Scans (Fase 2)
# ---------------------------------------------------------------------------

from monitor.scanner import scan_url as _scan_single, scan_all as _scan_all


def _process_rag_for_change(result: dict, url_data: dict):
    """When a URL changed, save content to Tutor DB and run RAG pipeline.

    Returns a dict with rag processing results (or empty on skip/failure).
    """
    text = result.get("_text")
    if not text:
        return {}

    status = result["status"]
    area_id = url_data.get("area_id")
    name = url_data.get("name", "Untitled")
    url = url_data["url"]

    import rag_wrapper as rw

    try:
        if status == "new":
            # First time: create doc in Tutor + RAG index
            doc_id = rw.create_document_in_tutor(
                name=f"{name} — {url}",
                area_id=area_id,
                content_text=text,
                url=url,
            )
            if not doc_id:
                return {"rag_ok": False, "error": "Could not create document"}

            rag_result = rw.process_document(doc_id)
            return {"rag_ok": True, "doc_id": doc_id, **rag_result}

        elif status == "changed":
            # URL already monitored — find existing Tutor docs for this area+URL
            import psycopg2
            db_cfg = {
                "dbname": "ai_tutor_db",
                "user": "postgres",
                "host": "/var/run/postgresql",
            }
            conn = psycopg2.connect(**db_cfg)
            try:
                cur = conn.cursor()
                # Find the most recent document for this area+URL
                cur.execute(
                    "SELECT id FROM documents WHERE area_id = %s AND url = %s ORDER BY upload_date DESC LIMIT 1",
                    (area_id, url),
                )
                row = cur.fetchone()
            finally:
                conn.close()

            if row:
                doc_id = row[0]
                rag_result = rw.reprocess_existing(doc_id, text)
                return {"rag_ok": True, "doc_id": doc_id, **rag_result}
            else:
                # No existing doc found — treat as new
                doc_id = rw.create_document_in_tutor(
                    name=f"{name} — {url}",
                    area_id=area_id,
                    content_text=text,
                    url=url,
                )
                if not doc_id:
                    return {"rag_ok": False, "error": "Could not create document"}
                rag_result = rw.process_document(doc_id)
                return {"rag_ok": True, "doc_id": doc_id, **rag_result}

    except Exception as e:
        print(f"RAG processing error for URL {url_data.get('id')}: {e}")
        return {"rag_ok": False, "error": str(e)}

    return {}


def persist_scan(result: dict, url_data: dict | None = None):
    """Save a scan result to DB, update URL hash, and optionally run RAG."""
    # Strip internal fields before saving (keep _text for RAG)
    text_for_rag = result.get("_text")
    clean = {k: v for k, v in result.items() if not k.startswith("_")}
    url_id = clean.pop("url_id")

    conn = get_db()
    try:
        cur = conn.cursor()
        # Insert scan record
        cur.execute(
            """INSERT INTO monitor_scans
               (url_id, content_hash, status, change_type, docs_created,
                docs_updated, duration_seconds, error_message)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                url_id,
                clean["content_hash"],
                clean["status"],
                clean["change_type"],
                clean["docs_created"],
                clean["docs_updated"],
                clean["duration_seconds"],
                clean.get("error_message"),
            ),
        )
        # Update URL's hash and last_fetched_at if not error
        if clean["status"] != "error":
            cur.execute(
                "UPDATE monitor_urls SET last_content_hash = %s, last_fetched_at = NOW() WHERE id = %s",
                (clean["content_hash"], url_id),
            )
        conn.commit()
    finally:
        conn.close()

    # RAG: if content changed and we have text + context, index it
    rag_info = {}
    if clean["status"] in ("new", "changed") and text_for_rag and url_data:
        rag_info = _process_rag_for_change(result, url_data)
        # Update scan record with actual RAG results
        if rag_info.get("rag_ok"):
            conn2 = get_db()
            try:
                cur2 = conn2.cursor()
                scan_id = clean.get("id")  # not available yet; update by url_id+hash instead
                cur2.execute(
                    """UPDATE monitor_scans SET docs_created = %s, docs_updated = %s,
                       error_message = CASE WHEN status = 'new' THEN 'rag_indexed'
                                           ELSE 'rag_reindexed' END
                    WHERE url_id = %s AND content_hash = %s AND id = (
                        SELECT max(id) FROM monitor_scans WHERE url_id = %s AND content_hash = %s
                    )""",
                    (rag_info.get("chunks_created", 0), rag_info.get("saved_count", 0),
                     url_id, clean["content_hash"], url_id, clean["content_hash"]),
                )
                conn2.commit()
            finally:
                conn2.close()

    clean.update(rag_info)
    return clean


@app.post("/scan/all")
def scan_all_route():
    """Scan all enabled URLs. Returns summary + per-URL results with RAG."""
    from monitor.url_registry import list_urls as _list_urls

    urls = _list_urls(enabled_only=True)
    raw_results = []
    url_map = {u["id"]: u for u in urls}

    for url_data in urls:
        result = _scan_single(url_data)
        raw_results.append((url_data, result))

    saved = []
    for url_data, r in raw_results:
        s = persist_scan(r, url_data=url_data)
        saved.append(s)

    total = len(saved)
    changed = sum(1 for s in saved if s["status"] != "unchanged")
    errors = sum(1 for s in saved if s["status"] == "error")

    return {
        "total_scanned": total,
        "changed": changed,
        "errors": errors,
        "scans": saved,
    }


@app.post("/scan/{url_id}")
def scan_url_route(url_id: int):
    """Scan a specific URL. Returns the scan result with RAG processing."""
    from monitor.url_registry import get_url as _get_url

    url_data = _get_url(url_id)
    if not url_data or not url_data.get("enabled"):
        raise HTTPException(404, "URL not found or disabled")

    result = _scan_single(url_data)
    return persist_scan(result, url_data=url_data)


@app.get("/scans")
def list_scans(limit: int = 50, url_id: int | None = None):
    """Scan history with optional URL filter."""
    conn = get_db()
    try:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if url_id:
            cur.execute(
                "SELECT * FROM monitor_scans WHERE url_id = %s ORDER BY scanned_at DESC LIMIT %s",
                (url_id, limit),
            )
        else:
            cur.execute("SELECT * FROM monitor_scans ORDER BY scanned_at DESC LIMIT %s", (limit,))
        rows = cur.fetchall()

        scans = []
        for r in rows:
            s = dict(r)
            # Format timestamp strings for JSON
            if s.get("scanned_at"):
                s["scanned_at"] = str(s["scanned_at"])
            scans.append(s)

        return {"scans": scans, "total": len(scans)}
    finally:
        conn.close()


@app.get("/stats")
def get_stats():
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Count URLs
        cur.execute("SELECT count(*) as total FROM monitor_urls WHERE enabled")
        url_count = cur.fetchone()["total"]
        # Count scans
        cur.execute("SELECT count(*) as total FROM monitor_scans")
        scan_count = cur.fetchone()["total"]
        return {
            "urls_registered": url_count,
            "scans_performed": scan_count,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes — Links & RAG (placeholders for Fase 3-4)
# ---------------------------------------------------------------------------

@app.get("/links")
def list_links(parent_url_id: int | None = None):
    """List extracted links from a monitored URL."""
    conn = get_db()
    try:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if parent_url_id:
            cur.execute(
                "SELECT * FROM monitor_extracted_links WHERE parent_url_id = %s ORDER BY name",
                (parent_url_id,),
            )
        else:
            cur.execute("SELECT * FROM monitor_extracted_links ORDER BY last_extracted_at DESC")
        rows = cur.fetchall()
        return {"links": [dict(r) for r in rows], "total": len(rows)}
    finally:
        conn.close()


@app.post("/links/extract")
def extract_links_route(url_id: int | None = None, url: str | None = None):
    """Extract downloadable links from a JS-rendered portal using Playwright."""
    if not url_id and not url:
        raise HTTPException(400, "Provide url_id or url")

    # Resolve URL from DB if url_id given
    if url_id and not url:
        from monitor.url_registry import get_url as _get_url
        url_data = _get_url(url_id)
        if not url_data:
            raise HTTPException(404, "URL not found")
        url = url_data["url"]
        parent_url_id = url_id
    else:
        parent_url_id = url_id

    from scraper.js_renderer import extract_links as _extract
    extracted = _extract(url)

    # Save to DB if we have a parent URL
    saved = 0
    if parent_url_id and extracted:
        conn = get_db()
        try:
            cur = conn.cursor()
            for link in extracted:
                cur.execute(
                    """INSERT INTO monitor_extracted_links
                       (parent_url_id, name, url, link_type, last_extracted_at)
                       VALUES (%s, %s, %s, %s, NOW())
                       ON CONFLICT (parent_url_id, url) DO UPDATE SET
                           name = EXCLUDED.name,
                           link_type = EXCLUDED.link_type,
                           last_extracted_at = NOW()
                       RETURNING id""",
                    (parent_url_id, link["name"], link["url"], link["type"]),
                )
                saved += cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()

    return {
        "source_url": url,
        "links_found": len(extracted),
        "links_saved": saved,
        "links": extracted,
    }


@app.post("/rag/process/{tutor_doc_id}")
def rag_process(tutor_doc_id: int):
    """Process a Tutor document through the RAG pipeline (chunk + embed)."""
    import rag_wrapper as rw

    doc = rw.get_document(tutor_doc_id)
    if not doc:
        raise HTTPException(404, f"Tutor document {tutor_doc_id} not found")

    result = rw.process_document(tutor_doc_id)
    return {"doc_id": tutor_doc_id, **result}


@app.get("/health/extended")
def health_extended():
    """Extended health, including whether the Tutor's RAG engine is reachable and loaded."""
    import rag_wrapper as rw

    return {
        "service": "monitor-agent-api",
        "db_connected": True,  # if we reach here, lifespan passed
        "rag_engine_available": rw.is_rag_available(),
    }


# ---------------------------------------------------------------------------
# Routes — Dashboard data
# ---------------------------------------------------------------------------

@app.get("/dashboard")
def dashboard_data():
    """All-in-one endpoint for the dashboard frontend."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # URLs with scan stats
        cur.execute(
            """SELECT u.*,
               (
                   SELECT count(*) FROM monitor_scans s WHERE s.url_id = u.id
               ) AS total_scans,
               (
                   SELECT count(*) FROM monitor_scans s WHERE s.url_id = u.id AND s.status != 'unchanged'
               ) AS change_count,
               (
                   SELECT max(s.scanned_at) FROM monitor_scans s WHERE s.url_id = u.id
               ) AS latest_scan,
               (
                   SELECT count(*) FROM monitor_extracted_links l WHERE l.parent_url_id = u.id
               ) AS extracted_links
            FROM monitor_urls u ORDER BY u.id"""
        )
        urls = [dict(r) for r in cur.fetchall()]
        # Serialize timestamps
        for u in urls:
            for field in ("last_fetched_at", "created_at", "latest_scan"):
                if u.get(field):
                    u[field] = str(u[field])

        # Recent scans (last 20)
        cur.execute(
            """SELECT s.*, u.name AS url_name, u.url AS url_value
               FROM monitor_scans s
               JOIN monitor_urls u ON u.id = s.url_id
               ORDER BY s.scanned_at DESC LIMIT 20"""
        )
        scans = [dict(r) for r in cur.fetchall()]
        for s in scans:
            if s.get("scanned_at"):
                s["scanned_at"] = str(s["scanned_at"])

        # Extracted links by parent URL
        cur.execute(
            """SELECT l.*, u.name AS parent_name
               FROM monitor_extracted_links l
               JOIN monitor_urls u ON u.id = l.parent_url_id
               ORDER BY l.parent_url_id, l.link_type, l.name"""
        )
        links = [dict(r) for r in cur.fetchall()]
        for l in links:
            if l.get("last_extracted_at"):
                l["last_extracted_at"] = str(l["last_extracted_at"])

        # Tutor RAG stats
        # (queries ai_tutor_db directly via same connection since it's the same DB)
        cur.execute(
            "SELECT count(*) AS total_docs FROM documents"
        )
        tutor_docs = cur.fetchone()["total_docs"]

        cur.execute(
            "SELECT count(*) AS total_chunks FROM document_chunks"
        )
        tutor_chunks = cur.fetchone()["total_chunks"]

        # Areas with doc counts
        cur.execute(
            """SELECT a.id, a.name, a.slug,
               (SELECT count(*) FROM documents d WHERE d.area_id = a.id) AS doc_count,
               (SELECT coalesce(sum(d.chunk_count), 0) FROM documents d WHERE d.area_id = a.id) AS chunk_count
             FROM areas a ORDER BY a.id"""
        )
        areas = [dict(r) for r in cur.fetchall()]

    finally:
        conn.close()

    return {
        "urls": urls,
        "recent_scans": scans,
        "extracted_links": links,
        "tutor_stats": {"documents": tutor_docs, "chunks": tutor_chunks},
        "areas": areas,
    }


# ---------------------------------------------------------------------------
# Routes — External Extract (for Tutor extract.html) — Fase 5 placeholder
# ---------------------------------------------------------------------------

class ExtractRequest(BaseModel):
    url: str
    area_id: int | None = None


@app.post("/api/external/extract")
def external_extract(req: ExtractRequest):
    """Endpoint that Tutor's extract.html calls instead of :5001."""
    return {"status": "not_implemented_yet", "phase": "Fase 5"}
