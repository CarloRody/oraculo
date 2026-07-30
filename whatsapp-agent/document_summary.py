"""Fila de resumo automático (via IA local com visão) de documentos de
paciente — ver connectors/document_ai.py pro cliente HTTP do modelo.

Mesmo idioma de booking_flow.py: `import server` tardio dentro das funções
que precisam de algo de lá (get_patient_document, log_event,
MEDIA_STORAGE_DIR), pra evitar ciclo de import (server.py importa este
módulo no topo).

O loop (document_summary_loop) roda em background — mesmo padrão de
_reminder_loop em server.py, um único threading.Thread daemon — só que
faz trabalho ATIVO a cada 10s (não só checa "due" a cada 5min), porque
existe uma fila de verdade pra esvaziar. Cada documento despachado vira
uma thread própria (mesmo padrão de _download_patient_media), então uma
falha/demora num documento nunca trava os outros nem o loop.

Por que respeitar velocidade: o MESMO modelo (LM Studio, ver config.yaml
"llm") também atende o chat da Oráculo em tempo real (ai_oraculo_saas).
Resumir documentos em lote não pode competir por throughput com respostas
de chat ao vivo — daí o freio por tokens/segundo (medido nas últimas
conclusões, não informado pelo modelo) além do paralelismo máximo.
"""

import os
import threading
import time

import psycopg2

from config import DB_CONFIG
import connectors.document_ai as document_ai

POLL_INTERVAL_SECONDS = 10
RECENT_SAMPLE_COUNT = 5


def _conn():
    return psycopg2.connect(**DB_CONFIG)


# ---------------------------------------------------------------------------
# PDF → imagem
# ---------------------------------------------------------------------------

def pdf_first_pages_to_png(pdf_bytes, max_pages=5, dpi=150):
    """Renderiza até max_pages páginas do PDF em PNG (bytes, mime_type) —
    usado quando o documento é application/pdf, já que o modelo só aceita
    imagem. PyMuPDF é self-contained (sem depender de binário externo tipo
    poppler), importante no host ARM64 com espaço/dependências enxutas."""
    import fitz  # PyMuPDF — import local: só quem processa PDF paga o custo

    images = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(dpi=dpi)
            images.append((pix.tobytes("png"), "image/png"))
    finally:
        doc.close()
    return images


# ---------------------------------------------------------------------------
# Configuração da fila (linha única, id=1)
# ---------------------------------------------------------------------------

def get_ai_summary_settings():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT max_parallelism, target_tokens_per_second, paused FROM whatsapp_ai_summary_settings WHERE id = 1"
        )
        row = cur.fetchone()
        if not row:
            return {"max_parallelism": 1, "target_tokens_per_second": None, "paused": False}
        return {
            "max_parallelism": row[0],
            "target_tokens_per_second": float(row[1]) if row[1] is not None else None,
            "paused": row[2],
        }
    finally:
        conn.close()


def set_ai_summary_settings(max_parallelism, target_tokens_per_second, paused):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_ai_summary_settings
               SET max_parallelism = %s, target_tokens_per_second = %s, paused = %s, updated_at = NOW()
               WHERE id = 1""",
            (max_parallelism, target_tokens_per_second, paused),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Métricas de throughput recente (base do freio de velocidade)
# ---------------------------------------------------------------------------

def _recent_avg_tokens_per_second(n=RECENT_SAMPLE_COUNT):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT ai_summary_tokens, ai_summary_elapsed_ms FROM whatsapp_patient_documents
               WHERE ai_summary_status = 'done' AND ai_summary_tokens IS NOT NULL
                 AND ai_summary_elapsed_ms IS NOT NULL AND ai_summary_elapsed_ms > 0
               ORDER BY ai_summary_completed_at DESC LIMIT %s""",
            (n,),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        rates = [tokens / (elapsed_ms / 1000.0) for tokens, elapsed_ms in rows]
        return sum(rates) / len(rates)
    finally:
        conn.close()


def _count_processing():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM whatsapp_patient_documents WHERE ai_summary_status = 'processing'")
        return cur.fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fila: reivindicar, marcar concluído/falho, tentar de novo
# ---------------------------------------------------------------------------

def _claim_next_pending(limit):
    """FOR UPDATE SKIP LOCKED reivindica atomicamente — evita duas rodadas
    do loop (ou dois processos, se algum dia rodar mais de um) pegarem o
    mesmo documento."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """WITH claimed AS (
                   SELECT id FROM whatsapp_patient_documents
                   WHERE status = 'stored' AND ai_summary_status = 'pending' AND NOT hidden
                   ORDER BY captured_at ASC LIMIT %s FOR UPDATE SKIP LOCKED
               )
               UPDATE whatsapp_patient_documents d SET ai_summary_status = 'processing'
               FROM claimed WHERE d.id = claimed.id RETURNING d.id""",
            (limit,),
        )
        ids = [r[0] for r in cur.fetchall()]
        conn.commit()
        return ids
    finally:
        conn.close()


def mark_summary_done(doc_id, text, tokens, elapsed_seconds):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_patient_documents
               SET ai_summary_status = 'done', ai_summary = %s, ai_summary_tokens = %s,
                   ai_summary_elapsed_ms = %s, ai_summary_error = NULL, ai_summary_completed_at = NOW()
               WHERE id = %s""",
            (text, tokens, int(elapsed_seconds * 1000), doc_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_summary_failed(doc_id, error):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_patient_documents
               SET ai_summary_status = 'failed', ai_summary_error = %s, ai_summary_completed_at = NOW()
               WHERE id = %s""",
            (str(error)[:2000], doc_id),
        )
        conn.commit()
    finally:
        conn.close()


def retry_document(doc_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_patient_documents SET ai_summary_status = 'pending', ai_summary_error = NULL
               WHERE id = %s AND ai_summary_status = 'failed'""",
            (doc_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Leitura pra página de administração
# ---------------------------------------------------------------------------

def list_queue(status=None, limit=50):
    conn = _conn()
    try:
        cur = conn.cursor()
        where = "WHERE d.ai_summary_status = %s" if status else ""
        params = (status, limit) if status else (limit,)
        cur.execute(
            f"""SELECT d.id, d.doc_type, d.captured_at, d.ai_summary_status, d.ai_summary,
                       d.ai_summary_error, d.ai_summary_tokens, d.ai_summary_elapsed_ms,
                       COALESCE(ct.name, ct.push_name) AS patient_name, ct.wa_id
                FROM whatsapp_patient_documents d
                LEFT JOIN whatsapp_contacts ct ON ct.id = d.contact_id
                {where}
                ORDER BY d.captured_at DESC LIMIT %s""",
            params,
        )
        cols = ["id", "doc_type", "captured_at", "ai_summary_status", "ai_summary", "ai_summary_error",
                "ai_summary_tokens", "ai_summary_elapsed_ms", "patient_name", "wa_id"]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["captured_at"] = d["captured_at"].isoformat() if d["captured_at"] else None
            rows.append(d)
        return rows
    finally:
        conn.close()


def get_queue_stats():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT ai_summary_status, COUNT(*) FROM whatsapp_patient_documents GROUP BY ai_summary_status")
        counts = {status: count for status, count in cur.fetchall()}
    finally:
        conn.close()
    return {
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "recent_avg_tokens_per_second": _recent_avg_tokens_per_second(),
    }


# ---------------------------------------------------------------------------
# Processamento de um documento
# ---------------------------------------------------------------------------

def _process_one_document(doc_id):
    import server  # tardio — ver docstring do módulo

    doc = None
    try:
        doc = server.get_patient_document(doc_id)
        if not doc or not doc.get("storage_path"):
            raise RuntimeError("Documento sem arquivo associado")

        base = os.path.realpath(server.MEDIA_STORAGE_DIR)
        full = os.path.realpath(os.path.join(base, doc["storage_path"]))
        if not (full == base or full.startswith(base + os.sep)) or not os.path.isfile(full):
            raise RuntimeError("Arquivo não encontrado no disco")

        with open(full, "rb") as f:
            raw_bytes = f.read()

        if doc.get("mime_type") == "application/pdf":
            images = pdf_first_pages_to_png(raw_bytes)
            if not images:
                raise RuntimeError("PDF sem páginas legíveis")
        else:
            images = [(raw_bytes, doc.get("mime_type") or "image/jpeg")]

        text, tokens, elapsed = document_ai.summarize_document(images)
        mark_summary_done(doc_id, text, tokens, elapsed)
    except Exception as e:
        mark_summary_failed(doc_id, str(e))
        account_id = doc.get("account_id") if doc else None
        server.log_event(account_id, "document_summary_failed", level="error",
                          detail={"document_id": doc_id, "error": str(e)})


# ---------------------------------------------------------------------------
# Loop de despacho
# ---------------------------------------------------------------------------

def _dispatch_pending_documents():
    settings = get_ai_summary_settings()
    if settings["paused"]:
        return
    slots = settings["max_parallelism"] - _count_processing()
    if slots <= 0:
        return
    target = settings["target_tokens_per_second"]
    if target is not None:
        recent = _recent_avg_tokens_per_second()
        if recent is not None and recent < target:
            return  # modelo abaixo da velocidade desejada — espera a próxima rodada
    for doc_id in _claim_next_pending(slots):
        threading.Thread(target=_process_one_document, args=(doc_id,), daemon=True).start()


def document_summary_loop():
    import server  # tardio — ver docstring do módulo

    while True:
        try:
            _dispatch_pending_documents()
        except Exception as e:
            server.log_event(None, "document_summary_loop_error", level="error", detail={"error": str(e)})
        time.sleep(POLL_INTERVAL_SECONDS)
