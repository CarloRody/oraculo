"""Monitor Agent API — FastAPI application (port :5003)."""

import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from config import CONFIG, DB_CONFIG, MONITOR_CONFIG

config = CONFIG  # mantém o nome usado no resto deste arquivo

# ---------------------------------------------------------------------------
# DB helper (sync, lightweight — no ORM)
# ---------------------------------------------------------------------------

import psycopg2
from psycopg2.extras import RealDictCursor


def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.cursor().execute("SET statement_timeout = '60s'")
    return conn


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

scheduler_task = None  # asyncio task do scan periódico — None se default_cron não configurado


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler_task
    # startup: run migrations if needed
    from db_migrations import migrate_if_needed
    migrate_if_needed()

    # startup: scan periódico (cron em monitor_agent.default_cron do
    # config.yaml) — antes disso só rodava manual, via clique em "Scan Tudo".
    cron_expr = MONITOR_CONFIG.get("default_cron")
    if cron_expr:
        from monitor.scheduler import run_scheduler
        scheduler_task = asyncio.create_task(run_scheduler(cron_expr, run_full_scan))
        print(f"[startup] Scan automático ligado — cron: {cron_expr}")
    else:
        print("[startup] monitor_agent.default_cron não configurado — scan automático desligado.")

    yield

    # shutdown: encerra o loop do scheduler de forma limpa
    if scheduler_task:
        scheduler_task.cancel()


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
        # no-cache — sem isso o navegador pode servir uma cópia velha do
        # dashboard sem revalidar com o servidor depois de um deploy novo.
        return FileResponse(str(index_path), headers={"Cache-Control": "no-cache"})
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


class CrawlStart(BaseModel):
    url_id: int
    max_depth: int = 3
    area_id: int | None = None


class CrawlAdvanceLink(BaseModel):
    name: str
    url: str
    type: str = "html"


class CrawlAdvance(BaseModel):
    parent_page_id: int
    links: list[CrawlAdvanceLink]


class MonitorSelection(BaseModel):
    page_ids: list[int]


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
        "port": MONITOR_CONFIG["server"]["port"],
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


def _process_rag_for_change(result: dict, url_data: dict, scan_id: int | None = None):
    """When a URL changed, save content to Tutor DB and run RAG pipeline —
    or, if a document already exists for this URL, queue the new content as
    a pending version instead of overwriting it (see document_versions;
    admin must review+apply via POST /versions/{id}/apply).

    Returns a dict with rag/version processing results (or empty on skip/failure).
    """
    text = result.get("_text")
    if not text:
        return {}

    area_id = url_data.get("area_id")
    name = url_data.get("name", "Untitled")
    url = url_data["url"]

    import rag_wrapper as rw

    try:
        # Decisão create-vs-update depende só de "o documento já existe pra
        # essa (area_id, url)?", não do status "new"/"changed" do scan —
        # sem isso, uma URL que já virou documento via crawl de árvore (ver
        # rag_wrapper.ingest_and_index) e só depois é promovida a
        # monitorada cairia sempre no caso "new" no primeiro scan (é a
        # primeira vez que MONITOR_URLS vê essa URL) e duplicaria o
        # documento já existente.
        existing_doc_id = rw.find_existing_document(area_id, url)
        if existing_doc_id:
            version_id = rw.create_pending_version(
                document_id=existing_doc_id,
                url_id=url_data.get("id"),
                scan_id=scan_id,
                content_text=text,
                content_hash=result.get("content_hash"),
            )
            if not version_id:
                return {"rag_ok": False, "error": "Failed to create pending version"}
            return {"version_pending": True, "version_id": version_id, "doc_id": existing_doc_id}

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
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
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
        scan_id = cur.fetchone()[0]
        # Update URL's hash and last_fetched_at if not error — done
        # immediately regardless of whether the change ends up applied or
        # rejected, so unreviewed content isn't re-flagged as "changed"
        # again on every subsequent scan.
        if clean["status"] != "error":
            cur.execute(
                "UPDATE monitor_urls SET last_content_hash = %s, last_fetched_at = NOW() WHERE id = %s",
                (clean["content_hash"], url_id),
            )
        conn.commit()
    finally:
        conn.close()

    clean["scan_id"] = scan_id

    # RAG: if content changed and we have text + context, index it (or queue
    # a pending version for review — see _process_rag_for_change)
    rag_info = {}
    if clean["status"] in ("new", "changed") and text_for_rag and url_data:
        rag_info = _process_rag_for_change(result, url_data, scan_id)
        # Update scan record with actual RAG/version results
        if rag_info.get("rag_ok") or rag_info.get("version_pending"):
            conn2 = get_db()
            try:
                cur2 = conn2.cursor()
                if rag_info.get("version_pending"):
                    cur2.execute(
                        "UPDATE monitor_scans SET docs_updated = 1, error_message = 'version_pending' WHERE id = %s",
                        (scan_id,),
                    )
                else:
                    cur2.execute(
                        """UPDATE monitor_scans SET docs_created = %s, docs_updated = %s,
                           error_message = CASE WHEN status = 'new' THEN 'rag_indexed'
                                               ELSE 'rag_reindexed' END
                        WHERE id = %s""",
                        (rag_info.get("chunks_created", 0), rag_info.get("saved_count", 0), scan_id),
                    )
                conn2.commit()
            finally:
                conn2.close()

    clean.update(rag_info)
    return clean


def run_full_scan():
    """Escaneia todas as URLs habilitadas. Compartilhada pela rota manual
    POST /scan/all e pelo scheduler automático (monitor/scheduler.py) —
    mesma lógica pros dois casos, só muda quem chama."""
    from monitor.url_registry import list_urls as _list_urls

    urls = _list_urls(enabled_only=True)
    raw_results = []

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


@app.post("/scan/all")
def scan_all_route():
    """Scan all enabled URLs. Returns summary + per-URL results with RAG."""
    return run_full_scan()


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


# ---------------------------------------------------------------------------
# Routes — Knowledge tree (recursive link crawl)
#
# Each reviewed page becomes its own Tutor document immediately (linked to
# its parent page's document via parent_doc_id) — there's no "combine into
# one document" step. monitor_crawls/monitor_crawl_pages only track the
# review session itself (depth, dedup, which Tutor doc each page became).
# ---------------------------------------------------------------------------

def _annotate_links_existing(links, area_id):
    """Marca cada link candidato com exists=True quando a URL já é documento
    nesta área — a UI usa isso pra mostrar o selo 'já existe'. Não reordena
    nem remove nada; só acrescenta o campo. Uma query em lote (não uma por
    link)."""
    import rag_wrapper as rw
    if not links:
        return links
    existing = rw.find_existing_urls(area_id, [l.get("url") for l in links])
    for l in links:
        l["exists"] = l.get("url") in existing
    return links


@app.post("/crawl/start")
def crawl_start(data: CrawlStart):
    """Start a knowledge-tree crawl from a monitored URL: fetch the root
    page, create its Tutor document right away, and return candidate links
    for the next level."""
    from monitor.url_registry import get_url as _get_url
    from monitor.crawler import fetch_page_for_crawl, root_netloc, MAX_DEPTH_CEILING
    import rag_wrapper as rw

    url_data = _get_url(data.url_id)
    if not url_data:
        raise HTTPException(404, "URL not found")

    max_depth = max(1, min(data.max_depth, MAX_DEPTH_CEILING))
    area_id = data.area_id or url_data.get("area_id")
    if not area_id:
        raise HTTPException(400, "area_id é obrigatório (a URL monitorada não tem área definida)")
    fetch_mode = url_data.get("fetch_mode", "http")
    root_url = url_data["url"]

    page = fetch_page_for_crawl(root_url, fetch_mode=fetch_mode)
    if not page or not page.get("text"):
        raise HTTPException(502, f"Não foi possível extrair conteúdo de {root_url}")

    doc_name = page.get("title") or url_data.get("name") or root_url
    result = rw.ingest_and_index(
        page["text"], doc_name, area_id,
        url=root_url, parent_doc_id=None, fetch_mode=fetch_mode
    )
    tutor_doc_id = result.get("doc_id")
    if not tutor_doc_id:
        raise HTTPException(502, f"Falha ao criar documento raiz na Tutor: {result.get('error')}")

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """INSERT INTO monitor_crawls (root_url_id, root_url, max_depth, fetch_mode, area_id)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (data.url_id, root_url, max_depth, fetch_mode, area_id),
        )
        crawl_id = cur.fetchone()["id"]

        cur.execute(
            """INSERT INTO monitor_crawl_pages (crawl_id, parent_page_id, url, title, depth, tutor_doc_id)
               VALUES (%s, NULL, %s, %s, 1, %s) RETURNING id""",
            (crawl_id, root_url, doc_name, tutor_doc_id),
        )
        root_page_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    return {
        "crawl_id": crawl_id,
        "max_depth": max_depth,
        "domain": root_netloc(root_url),
        "root_page": {
            "id": root_page_id, "url": root_url, "title": doc_name,
            "depth": 1, "tutor_doc_id": tutor_doc_id,
        },
        "links": _annotate_links_existing(page.get("links", []), area_id),
    }


@app.post("/crawl/{crawl_id}/advance")
def crawl_advance(crawl_id: int, data: CrawlAdvance):
    """Fetch the selected child links, create a Tutor document for each
    (parent_doc_id = the page that linked to it), and return the fetched
    pages plus their candidate links for the next level."""
    from monitor.crawler import fetch_page_for_crawl, is_same_domain, root_netloc, MAX_PAGES_PER_CRAWL
    import rag_wrapper as rw

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM monitor_crawls WHERE id = %s", (crawl_id,))
        crawl = cur.fetchone()
        if not crawl:
            raise HTTPException(404, "Crawl not found")
        if crawl["status"] != "in_progress":
            raise HTTPException(400, f"Crawl já está '{crawl['status']}'")

        cur.execute(
            "SELECT * FROM monitor_crawl_pages WHERE id = %s AND crawl_id = %s",
            (data.parent_page_id, crawl_id),
        )
        parent_page = cur.fetchone()
        if not parent_page:
            raise HTTPException(404, "Parent page not found in this crawl")

        next_depth = parent_page["depth"] + 1
        if next_depth > crawl["max_depth"]:
            raise HTTPException(400, "Profundidade máxima já atingida")

        cur.execute("SELECT count(*) AS n FROM monitor_crawl_pages WHERE crawl_id = %s", (crawl_id,))
        total_pages = cur.fetchone()["n"]

        domain = root_netloc(crawl["root_url"])
        fetched = []
        for link in data.links:
            if total_pages >= MAX_PAGES_PER_CRAWL:
                break
            if crawl["same_domain_only"] and not is_same_domain(link.url, domain):
                continue
            cur.execute(
                "SELECT id FROM monitor_crawl_pages WHERE crawl_id = %s AND url = %s",
                (crawl_id, link.url),
            )
            if cur.fetchone():
                continue  # já visitado nesse crawl

            page = fetch_page_for_crawl(link.url, fetch_mode=crawl["fetch_mode"])
            if not page or not page.get("text"):
                continue

            doc_name = page.get("title") or link.name or link.url
            result = rw.ingest_and_index(
                page["text"], doc_name, crawl["area_id"],
                url=link.url, parent_doc_id=parent_page["tutor_doc_id"], fetch_mode=crawl["fetch_mode"]
            )
            tutor_doc_id = result.get("doc_id")
            if not tutor_doc_id:
                continue

            cur.execute(
                """INSERT INTO monitor_crawl_pages (crawl_id, parent_page_id, url, title, depth, tutor_doc_id)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (crawl_id, data.parent_page_id, link.url, doc_name, next_depth, tutor_doc_id),
            )
            page_id = cur.fetchone()["id"]
            total_pages += 1

            fetched.append({
                "id": page_id, "url": link.url, "title": doc_name,
                "depth": next_depth, "tutor_doc_id": tutor_doc_id,
                "links": _annotate_links_existing(
                    page.get("links", []) if next_depth < crawl["max_depth"] else [],
                    crawl["area_id"],
                ),
            })

        conn.commit()
    finally:
        conn.close()

    return {
        "crawl_id": crawl_id,
        "depth": next_depth,
        "reached_max_depth": next_depth >= crawl["max_depth"],
        "pages": fetched,
    }


@app.post("/crawl/{crawl_id}/finalize")
def crawl_finalize(crawl_id: int):
    """Mark a crawl session as done. Documents were already created
    incrementally during /advance — there's nothing left to build."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """UPDATE monitor_crawls SET status = 'completed', finished_at = NOW()
               WHERE id = %s AND status = 'in_progress' RETURNING id""",
            (crawl_id,),
        )
        if not cur.fetchone():
            raise HTTPException(404, "Crawl not found or already finished")
        conn.commit()

        cur.execute("SELECT count(*) AS n FROM monitor_crawl_pages WHERE crawl_id = %s", (crawl_id,))
        total = cur.fetchone()["n"]
    finally:
        conn.close()

    return {"crawl_id": crawl_id, "status": "completed", "total_pages": total}


@app.post("/crawl/{crawl_id}/cancel")
def crawl_cancel(crawl_id: int):
    """Cancel a crawl in progress and delete every Tutor document already
    created in this session (chunks included) — no leftover documents."""
    import rag_wrapper as rw

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM monitor_crawls WHERE id = %s", (crawl_id,))
        crawl = cur.fetchone()
        if not crawl:
            raise HTTPException(404, "Crawl not found")
        if crawl["status"] != "in_progress":
            raise HTTPException(400, f"Crawl já está '{crawl['status']}'")

        cur.execute(
            "SELECT tutor_doc_id FROM monitor_crawl_pages WHERE crawl_id = %s AND tutor_doc_id IS NOT NULL",
            (crawl_id,),
        )
        doc_ids = [r["tutor_doc_id"] for r in cur.fetchall()]
    finally:
        conn.close()

    deleted = sum(1 for doc_id in doc_ids if rw.delete_document(doc_id))

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE monitor_crawls SET status = 'cancelled', finished_at = NOW() WHERE id = %s",
            (crawl_id,),
        )
        conn.commit()
    finally:
        conn.close()

    return {"crawl_id": crawl_id, "status": "cancelled", "documents_deleted": deleted}


@app.get("/crawl/{crawl_id}")
def crawl_detail(crawl_id: int):
    """Full tree detail for a crawl session — used to reload the review UI
    without losing state."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM monitor_crawls WHERE id = %s", (crawl_id,))
        crawl = cur.fetchone()
        if not crawl:
            raise HTTPException(404, "Crawl not found")
        cur.execute(
            "SELECT * FROM monitor_crawl_pages WHERE crawl_id = %s ORDER BY depth, id",
            (crawl_id,),
        )
        pages = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return {"crawl": dict(crawl), "pages": pages}


@app.post("/crawl/{crawl_id}/monitor")
def crawl_add_to_monitoring(crawl_id: int, data: MonitorSelection):
    """Registra páginas escolhidas da árvore (pai e/ou filhos específicos)
    em monitor_urls — só a partir daqui elas passam a ser reconferidas pelo
    scan_all periódico. Antes disso, uma página crawleada é só uma
    ingestão única, nunca mais reconferida. area_id/fetch_mode vêm do
    próprio crawl (uniformes pra sessão inteira)."""
    from monitor.url_registry import add_url

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT area_id, fetch_mode FROM monitor_crawls WHERE id = %s", (crawl_id,))
        crawl = cur.fetchone()
        if not crawl:
            raise HTTPException(404, "Crawl not found")
        cur.execute(
            "SELECT id, url, title FROM monitor_crawl_pages WHERE crawl_id = %s AND id = ANY(%s)",
            (crawl_id, data.page_ids),
        )
        pages = cur.fetchall()
    finally:
        conn.close()

    added, skipped = [], []
    for p in pages:
        try:
            row = add_url(
                name=p["title"] or p["url"], url=p["url"],
                area_id=crawl["area_id"], fetch_mode=crawl["fetch_mode"], enabled=True,
            )
            added.append(row["id"])
        except ValueError:
            skipped.append(p["url"])  # já estava registrada em monitor_urls
    return {"added": len(added), "skipped": skipped}


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
        "scheduler": {
            "cron": MONITOR_CONFIG.get("default_cron"),
            "running": bool(scheduler_task) and not scheduler_task.done(),
        },
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

        # Mudanças pendentes de revisão — alimenta o badge do painel
        cur.execute("SELECT count(*) AS n FROM document_versions WHERE status = 'pending'")
        pending_versions_count = cur.fetchone()["n"]

    finally:
        conn.close()

    return {
        "urls": urls,
        "recent_scans": scans,
        "extracted_links": links,
        "tutor_stats": {"documents": tutor_docs, "chunks": tutor_chunks},
        "areas": areas,
        "pending_versions_count": pending_versions_count,
    }


# ---------------------------------------------------------------------------
# Routes — Document Versions (revisão de mudanças detectadas)
#
# Quando um scan detecta que um documento já existente mudou, o conteúdo
# novo vira uma versão 'pending' aqui (ver _process_rag_for_change) em vez
# de sobrescrever na hora. O admin revisa o diff contra o conteúdo atual e
# decide aplicar ou rejeitar.
# ---------------------------------------------------------------------------

@app.get("/versions")
def list_versions(status: str | None = None, document_id: int | None = None, limit: int = 100):
    """Lista versões (sem content_text inteiro — pode ser um PDF grande,
    mantém o payload leve). Usado tanto pelo painel de pendências
    (?status=pending) quanto pelo histórico por documento (?document_id=)."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        clauses = []
        params: list = []
        if status:
            clauses.append("dv.status = %s")
            params.append(status)
        if document_id:
            clauses.append("dv.document_id = %s")
            params.append(document_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cur.execute(
            f"""SELECT dv.id, dv.document_id, dv.url_id, dv.scan_id, dv.content_hash, dv.status,
                       dv.detected_at, dv.reviewed_at, length(dv.content_text) AS content_length,
                       d.name AS document_name, d.url AS document_url,
                       u.name AS monitor_url_name
                FROM document_versions dv
                JOIN documents d ON d.id = dv.document_id
                LEFT JOIN monitor_urls u ON u.id = dv.url_id
                {where}
                ORDER BY dv.detected_at DESC LIMIT %s""",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            for field in ("detected_at", "reviewed_at"):
                if r.get(field):
                    r[field] = str(r[field])
        return {"versions": rows, "total": len(rows)}
    finally:
        conn.close()


@app.get("/versions/{version_id}/diff")
def version_diff(version_id: int):
    """Diff por palavra entre a versão e o conteúdo atual (última versão
    aprovada) do documento. O texto extraído (HTML ou PDF) já vem sem
    quebras de linha — normalizado em espaço único por extract_text()/
    extract_pdf_text() — então diff por linha trataria o documento inteiro
    como uma única linha; diff por palavra (SequenceMatcher sobre tokens)
    é o que realmente produz segmentos úteis pra colorir na tela."""
    import difflib

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """SELECT dv.*, d.name AS document_name, d.content_text AS live_content_text
               FROM document_versions dv JOIN documents d ON d.id = dv.document_id
               WHERE dv.id = %s""",
            (version_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, "Version not found")

    live_tokens = (row["live_content_text"] or "").split()
    version_tokens = (row["content_text"] or "").split()
    # autojunk=False: o heurístico padrão do SequenceMatcher assume código-fonte
    # (penaliza tokens muito repetidos), o que distorce diffs de texto em
    # linguagem natural com palavras comuns repetidas várias vezes.
    sm = difflib.SequenceMatcher(None, live_tokens, version_tokens, autojunk=False)

    segments = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            segments.append({"op": "equal", "text": " ".join(live_tokens[i1:i2])})
        else:
            if i1 != i2:
                segments.append({"op": "delete", "text": " ".join(live_tokens[i1:i2])})
            if j1 != j2:
                segments.append({"op": "insert", "text": " ".join(version_tokens[j1:j2])})

    return {
        "version_id": version_id,
        "document_id": row["document_id"],
        "document_name": row["document_name"],
        "status": row["status"],
        "detected_at": str(row["detected_at"]) if row["detected_at"] else None,
        "diff": segments,
        "live_length": len(row["live_content_text"] or ""),
        "version_length": len(row["content_text"] or ""),
    }


@app.post("/versions/{version_id}/apply")
def apply_version(version_id: int):
    """Aplica a versão pendente: sobrescreve documents.content_text e
    reprocessa via rag_wrapper.reprocess_existing() — a mesma função que já
    fazia essa sobrescrita direto antes desta feature existir, reaproveitada
    aqui em vez de duplicada."""
    import rag_wrapper as rw

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM document_versions WHERE id = %s", (version_id,))
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, "Version not found")
    if row["status"] != "pending":
        raise HTTPException(409, f"Version already '{row['status']}'")

    rag_result = rw.reprocess_existing(row["document_id"], row["content_text"])

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE document_versions SET status='applied', reviewed_at=NOW() WHERE id=%s",
            (version_id,),
        )
        conn.commit()
    finally:
        conn.close()

    return {"applied": True, "document_id": row["document_id"], **rag_result}


@app.post("/versions/{version_id}/reject")
def reject_version(version_id: int):
    """Rejeita a versão pendente — documents.content_text nunca é tocado
    aqui (só apply_version escreve nele)."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """UPDATE document_versions SET status='rejected', reviewed_at=NOW()
               WHERE id=%s AND status='pending' RETURNING *""",
            (version_id,),
        )
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT status FROM document_versions WHERE id=%s", (version_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(404, "Version not found")
            raise HTTPException(409, f"Version already '{existing['status']}'")
        conn.commit()
        return {"rejected": True, "document_id": row["document_id"]}
    finally:
        conn.close()


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
