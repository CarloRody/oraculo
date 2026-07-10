"""RAG Engine - Pipeline completo de ingestão e busca por embeddings."""

import json
import re
from pathlib import Path
from typing import Optional, Union

import psycopg2
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer

# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------
MODEL_NAME = "all-MiniLM-L6-v2"  # leve e eficaz para ARM64
model = None


def get_model():
    global model
    if model is None:
        model = SentenceTransformer(MODEL_NAME)
    return model


# ---------------------------------------------------------------------------
# Conexão DB
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "dbname": "ai_tutor_db",
    "user": "postgres",
    "host": "/var/run/postgresql",
}


def get_conn():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        print(f"ERRO DB: {e}")
        return None


# ---------------------------------------------------------------------------
# 1. Extrair texto de URL (link externo)
# ---------------------------------------------------------------------------
def fetch_url_text(url: str, timeout=30) -> Optional[str]:
    """Baixa a página via HTTP simples e extrai o corpo principal como texto limpo.
    Não executa JavaScript — para portais SPA/ASP.NET que renderizam conteúdo
    via JS, usar fetch_url_text_js (fetch_mode='js_browser')."""
    try:
        res = requests.get(url, timeout=timeout, headers={"User-Agent": "AI-Tutor/1.0"})
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        # Remove scripts e estilos
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        text = soup.get_text(separator=" ", strip=True)
        # Normaliza whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text if len(text) > 50 else None
    except Exception as e:
        print(f"Erro ao fetch {url}: {e}")
        return None


def fetch_url_text_js(url: str, timeout=60) -> Optional[str]:
    """Abre a URL num Chromium headless (Playwright) e extrai o texto após o
    JavaScript rodar — mesma técnica usada no Monitor Agent para portais que
    não têm conteúdo no HTML bruto (SPA, ASP.NET). Import feito sob demanda
    para não travar o serviço inteiro se o Playwright não estiver instalado."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("fetch_url_text_js: Playwright não instalado neste venv.")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=(
                "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ))
            page = context.new_page()
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            html = str(page.content())
            browser.close()

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        return text if len(text) > 50 else None
    except Exception as e:
        print(f"Erro ao fetch (js_browser) {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# 1b. Extrair texto de PDF
# ---------------------------------------------------------------------------
def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    """Extrai texto de um PDF usando pypdf."""
    try:
        from io import BytesIO
        reader = PdfReader(BytesIO(pdf_bytes))
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
        full_text = "\n".join(pages_text)
        # Normaliza whitespace
        full_text = re.sub(r"\s+", " ", full_text).strip()
        return full_text if len(full_text) > 50 else None
    except Exception as e:
        print(f"Erro ao extrair PDF: {e}")
        return None


# ---------------------------------------------------------------------------
# 2. Chunking com sentence boundary detection (≈500 tokens ≈ ~350-400 chars)
# ---------------------------------------------------------------------------
def chunk_text(text: str, max_chars=400, overlap=80) -> list[dict]:
    """Divide texto em chunks mantendo sentenças intactas com overlap.

    Cada chunk novo inclui as últimas `overlap` chars do anterior para manter contexto contínuo.
    """
    sentences = re.split(r"(?<=[.!?;])\s+", text.strip())

    chunks: list[tuple[str, int]] = []  # (text, start_pos)
    current_chunk = ""
    chunk_start_idx = 0

    for i, sent in enumerate(sentences):
        if len(current_chunk) + len(sent) > max_chars and current_chunk:
            chunks.append((current_chunk.strip(), chunk_start_idx))

            # Overlap: mantém os últimos chars do chunk anterior no próximo
            overlap_text = current_chunk[-overlap:] if overlap > 0 else ""
            # Se o overlap corta uma palavra, volta até a última pontuação de frase
            if overlap > 0:
                last_dot = overlap_text.rfind(". ")
                if last_dot > 10:  # só ajusta se tem espaço suficiente
                    overlap_text = overlap_text[last_dot + 2:]
            current_chunk = overlap_text
            chunk_start_idx = i
        current_chunk += sent + " "

    if current_chunk.strip():
        chunks.append((current_chunk.strip(), chunk_start_idx))

    return [{"chunk_text": t, "start_index": idx} for t, idx in chunks]


# ---------------------------------------------------------------------------
# 3. Gerar embeddings (via sentence-transformers)
# ---------------------------------------------------------------------------
def generate_embeddings(chunks: list[str]) -> list[list[float]]:
    """Retorna lista de vetores embedding para cada chunk."""
    emb_model = get_model()
    vectors = emb_model.encode(
        chunks,
        show_progress_bar=False,
        normalize_embeddings=True  # normaliza os embeddings (L2)
    ).tolist()
    return vectors


# ---------------------------------------------------------------------------
# 4. Salvar chunks + embeddings no Postgres
# ---------------------------------------------------------------------------
def save_chunks_to_db(doc_id: int, area_id: int, chunks_data: list[dict], embedding_vectors: list[list[float]]) -> dict:
    """Insere chunks e embeddings na tabela document_chunks."""
    conn = get_conn()
    if not conn:
        return {"ok": False, "error": "Não conectou ao banco"}

    try:
        cur = conn.cursor()
        now = "NOW()"
        # Limpa chunks antigos deste documento (se houver)
        cur.execute("DELETE FROM document_chunks WHERE doc_id = %s", (doc_id,))

        for idx, chunk_data in enumerate(chunks_data):
            text = chunk_data["chunk_text"]
            vector_json = json.dumps(embedding_vectors[idx]) if embedding_vectors else None

            cur.execute(
                """INSERT INTO document_chunks (doc_id, area_id, content_chunk, chunk_index, embedding_vector)
                   VALUES (%s, %s, %s, %s, %s)""",
                (doc_id, area_id, text, idx, vector_json),
            )

        conn.commit()
        total = len(chunks_data)
        cur.execute("SELECT count(*) FROM document_chunks WHERE doc_id = %s", (doc_id,))
        saved_count = cur.fetchone()[0]
        conn.close()
        return {"ok": True, "chunks_created": total, "saved_count": saved_count}

    except Exception as e:
        if conn:
            conn.rollback()
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 5. Buscar por similaridade (cosine) via SQL
# ---------------------------------------------------------------------------
def search_similar(query_text: str, area_ids: Optional[list[int]] = None, top_k=4) -> list[dict]:
    """Busca chunks similares à query via cosine similarity calculada em Python.

    Como pgvector não está disponível no ARM64 deste host,
    busca todos os chunks da(s) área(s) e ordena por cosine distance local.

    area_ids=None ou [] busca em todas as áreas. Com uma área só, pega os
    `top_k` melhores globais (comportamento de sempre). Com várias áreas,
    reserva uma fatia de `top_k` para cada área (representação garantida),
    em vez de rankear tudo junto — evita que uma área "suma" da resposta
    por ter notas de similaridade mais baixas que outra.
    """
    conn = get_conn()
    if not conn:
        return []

    try:
        emb_model = get_model()
        query_vector = emb_model.encode([query_text])[0].tolist()

        # Busca todos os chunks da(s) área(s) (ou de todas se area_ids vazio)
        where_clause = "WHERE dc.area_id = ANY(%s)" if area_ids else ""
        params = [list(area_ids)] if area_ids else []

        sql = f"""
            SELECT id, doc_id, area_id, content_chunk, chunk_index, embedding_vector
            FROM document_chunks dc {where_clause}
        """

        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

        # Calcula cosine similarity em Python para cada chunk
        scored = []
        for row in rows:
            try:
                chunk_vector = json.loads(row[5])
                # Cosine similarity: dot(a,b) / (|a| * |b|)
                dot = sum(a * b for a, b in zip(query_vector, chunk_vector))
                norm_q = sum(x * x for x in query_vector) ** 0.5
                norm_c = sum(x * x for x in chunk_vector) ** 0.5
                if norm_q > 0 and norm_c > 0:
                    similarity = dot / (norm_q * norm_c)
                else:
                    similarity = 0.0
            except Exception:
                # Se embedding_vector é inválido, ignora o chunk
                continue

            scored.append({
                "chunk_id": row[0],
                "doc_id": row[1],
                "area_id": row[2],
                "content_chunk": row[3],
                "chunk_index": row[4],
                "distance": round(1.0 - similarity, 4),
            })

        if area_ids and len(area_ids) > 1:
            # Várias áreas: reserva uma fatia de top_k por área, agrupado por área
            per_area_k = max(1, top_k // len(area_ids))
            results = []
            for aid in area_ids:
                area_scored = [s for s in scored if s["area_id"] == aid]
                area_scored.sort(key=lambda x: x["distance"])
                results.extend(area_scored[:per_area_k])
        else:
            # Uma área só (ou nenhuma) — ranking global, como sempre foi
            scored.sort(key=lambda x: x["distance"])
            results = scored[:top_k]

        conn.close()
        return results

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"ERRO search_similar: {e}")
        return []


# ---------------------------------------------------------------------------
# 6. Pipeline completo: processar documento por ID
# ---------------------------------------------------------------------------
def process_document(doc_id: int) -> dict:
    """Pipeline completo: busca conteúdo (se link externo), chunk, embed e salva."""
    conn = get_conn()
    if not conn:
        return {"ok": False, "error": "Não conectou ao banco"}

    try:
        cur = conn.cursor()

        # Busca documento
        cur.execute("SELECT id, area_id, is_external_link, url, content_text, fetch_mode FROM documents WHERE id = %s", (doc_id,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": f"Documento {doc_id} não encontrado"}

        doc_id_val, area_id_val, is_external, url, existing_content, fetch_mode = row[0], row[1], row[2], row[3], row[4], row[5]

        # Se é link externo e ainda não tem conteúdo, busca da URL.
        # fetch_mode='js_browser' usa um Chromium headless (Playwright) para
        # portais que só renderizam conteúdo via JavaScript; 'http' (padrão)
        # é o fetch simples de sempre.
        if is_external and not existing_content:
            text = fetch_url_text_js(url) if fetch_mode == "js_browser" else fetch_url_text(url)  # type: ignore[arg-type]
            if not text:
                return {"ok": False, "error": f"Não foi possível obter texto da URL: {url}"}
            # Salva conteúdo extraído na tabela documents
            cur.execute("UPDATE documents SET content_text = %s WHERE id = %s", (text, doc_id_val))
            conn.commit()
            existing_content = text  # Atualiza variável para usar no chunking

        text = existing_content if existing_content else ""
        if not text:
            return {"ok": False, "error": f"Documento {doc_id} não tem conteúdo para processar"}

        # Chunks
        chunks_data = chunk_text(text)
        if len(chunks_data) == 0:
            return {"ok": True, "chunks_created": 0, "saved_count": 0}

        chunk_texts = [c["chunk_text"] for c in chunks_data]

        # Embeddings
        embedding_vectors = generate_embeddings(chunk_texts)

        # Salva no banco
        result = save_chunks_to_db(doc_id_val, area_id_val, chunks_data, embedding_vectors)

        # Atualiza status do documento após processamento
        if result.get("ok"):
            cur.execute(
                """UPDATE documents SET processing_status = 'indexed',
                           chunk_count = %s,
                           last_processed_at = NOW()
                    WHERE id = %s""",
                (len(chunks_data), doc_id_val),
            )
            conn.commit()
        else:
            cur.execute(
                "UPDATE documents SET processing_status = 'failed' WHERE id = %s",
                (doc_id_val,),
            )
            conn.commit()

        return {**result, "doc_id": doc_id_val, "area_id": area_id_val}

    except Exception as e:
        if conn:
            conn.rollback()
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# CLI (para testar via terminal)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "process":
        doc_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
        if not doc_id:
            print("Uso: python rag_engine.py process <doc_id>")
            sys.exit(1)
        result = process_document(doc_id)
        print(json.dumps(result, indent=2))

    elif cmd == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        area_id = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None
        results = search_similar(query, area_ids=[area_id] if area_id else None)
        print(json.dumps(results[:10], indent=2, ensure_ascii=False))

    elif cmd == "help":
        print("""
RAG Engine - AI Tutor SaaS
Uso:
  python rag_engine.py process <doc_id>   → Processa um documento (fetch + chunk + embed)
  python rag_engine.py search "<pergunta>" [area_id]  → Busca similaridade nos chunks
""")
