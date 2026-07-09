from flask import Flask, jsonify, request, send_from_directory
import psycopg2
import json
import os
import requests as _http_requests
from flask_cors import CORS

# Importar RAG engine (pipeline completo: fetch → chunk com overlap → embed → salva)
import sys, os as _os_module
sys.path.insert(0, _os_module.path.join(_os_module.path.dirname(__file__), '..'))
from rag_engine import process_document, search_similar, get_model, extract_pdf_text
from migrations import migrate_if_needed

app = Flask(__name__)
CORS(app)  # Permite que o frontend acesse de qualquer origem local


def get_db_connection():
    """Conecta ao banco de dados PostgreSQL via socket Unix."""
    try:
        conn = psycopg2.connect(
            dbname="ai_tutor_db",
            user="postgres",
            host="/var/run/postgresql"  # Socket Unix — evita scram-sha-256
        )
        return conn
    except Exception as e:
        print(f"Erro ao conectar com o banco: {e}")
        return None


@app.route('/api/areas', methods=['GET'])
def get_areas():
    """Retorna todas as áreas ativas do banco."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Não foi possível conectar ao banco de dados"}), 500

    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, slug FROM areas WHERE status = 'active' ORDER BY name")
        rows = cur.fetchall()
        areas = [{"id": r[0], "name": r[1], "slug": r[2]} for r in rows]

        conn.close()
        return jsonify({"areas": areas})

    except Exception as e:
        if conn:
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/api/documents', methods=['GET'])
def get_documents():
    """Retorna todos os documentos de uma área ou todos."""
    area_id = request.args.get('area_id')
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Não foi possível conectar ao banco de dados"}), 500

    try:
        cur = conn.cursor()
        if area_id:
            cur.execute(
                """SELECT id, name, is_external_link, url, processing_status, chunk_count
                   FROM documents WHERE area_id = %s ORDER BY upload_date DESC""",
                (area_id,)
            )
        else:
            cur.execute(
                """SELECT id, name, is_external_link, url, processing_status, chunk_count
                   FROM documents ORDER BY upload_date DESC"""
            )

        rows = cur.fetchall()
        docs = []
        for r in rows:
            doc_type = "link" if r[2] else "file"
            docs.append({
                "id": r[0], "name": r[1], "type": doc_type, "url": r[3] or "",
                "processing_status": r[4] or "pending",
                "chunk_count": r[5] or 0
            })

        conn.close()
        return jsonify({"documents": docs})

    except Exception as e:
        if conn:
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/api/documents', methods=['POST'])
def create_document():
    """Cria um novo documento e processa RAG automaticamente."""
    data = request.get_json()
    area_id = data.get('area_id')
    url = data.get('url')
    is_external = data.get('is_external_link', False)
    content_text = data.get('content_text', '')  # Texto direto (para uploads de arquivo)

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Não foi possível conectar ao banco de dados"}), 500

    try:
        cur = conn.cursor()

        # INSERT básico com RETURNING
        cur.execute(
            """INSERT INTO documents (area_id, name, url, is_external_link, status, processing_status, last_checked_at, upload_date)
               VALUES (%s, %s, %s, %s, 'active', 'pending', NOW(), NOW()) RETURNING id""",
            (area_id, data.get('name', 'Documento sem nome'), url if url else None, is_external)
        )
        doc_id = cur.fetchone()[0]

        # Se foi enviado texto direto, salva no banco antes de processar
        if content_text and not is_external:
            cur.execute("UPDATE documents SET content_text = %s WHERE id = %s", (content_text, doc_id))

        conn.commit()
        conn.close()

        # Processar RAG automaticamente (fetch URL se externo → chunk com overlap → embed → salva)
        result = process_document(doc_id)

        return jsonify({
            "id": doc_id,
            "message": "Documento criado com sucesso",
            "rag_result": result
        }), 201

    except Exception as e:
        if conn:
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/api/process/<int:doc_id>', methods=['POST'])
def process_rag(doc_id):
    """Processa RAG de um documento existente (fetch + chunk + embed)."""
    result = process_document(doc_id)

    if not result.get("ok"):
        return jsonify({"error": result.get("error", "Erro desconhecido")}), 500

    return jsonify({
        "message": f"Documento {doc_id} processado com sucesso",
        "chunks_created": result.get("chunks_created", 0),
        "saved_count": result.get("saved_count", 0)
    })


@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Retorna contagem de documentos, áreas e chunks + status do banco."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Não foi possível conectar ao banco", "db_connected": False}), 500

    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM areas WHERE status = 'active'")
        area_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM documents")
        doc_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM document_chunks")
        chunk_count = cur.fetchone()[0]

        # Status do modelo RAG
        try:
            get_model()  # força carregamento
            model_status = "loaded"
        except Exception as e:
            model_status = f"error: {e}"

        conn.close()
        return jsonify({
            "db_connected": True,
            "area_count": area_count,
            "doc_count": doc_count,
            "chunk_count": chunk_count,
            "rag_model": model_status
        })

    except Exception as e:
        if conn:
            conn.close()
        return jsonify({"error": str(e), "db_connected": False}), 500


@app.route('/api/search', methods=['POST'])
def rag_search():
    """Busca semântica RAG via embeddings + cosine similarity no Postgres."""
    data = request.get_json()
    query = data.get('query', '')
    area_id = data.get('area_id')
    try:
        top_k = max(1, min(int(data.get('top_k') or 8), 20))
    except (TypeError, ValueError):
        top_k = 8

    if not query:
        return jsonify({"error": "Campo 'query' é obrigatório"}), 400

    try:
        results = search_similar(query, area_id=area_id, top_k=top_k)

        # Enrich com nome do documento
        enriched = []
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            doc_ids = list(set(r["doc_id"] for r in results))
            if doc_ids:
                placeholders = ",".join(["%s"] * len(doc_ids))
                cur.execute(
                    f"SELECT id, name FROM documents WHERE id IN ({placeholders})",
                    doc_ids
                )
                doc_names = {row[0]: row[1] for row in cur.fetchall()}

            for r in results:
                similarity = round(1.0 - r["distance"], 4)
                enriched.append({
                    "chunk_id": r["chunk_id"],
                    "doc_id": r["doc_id"],
                    "doc_name": doc_names.get(r["doc_id"], "Desconhecido"),
                    "area_id": r["area_id"],
                    "content_chunk": r["content_chunk"],
                    "chunk_index": r["chunk_index"],
                    "similarity": similarity,
                    "distance": round(r["distance"], 4)
                })
            conn.close()

        return jsonify({"results": enriched, "query": query})

    except Exception as e:
        print(f"ERRO rag_search: {e}")
        # Fallback: busca por texto simples se RAG falhar
        try:
            conn = get_db_connection()
            if not conn:
                return jsonify({"error": "RAG indisponível, fallback também falhou"}), 500
            cur = conn.cursor()
            where_clause = "WHERE d.area_id = %s" if area_id else ""
            params = [area_id] if area_id else []

            cur.execute(
                f"""SELECT dc.id, d.name as doc_name, dc.content_chunk, dc.chunk_index
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.doc_id
                    {where_clause}
                    WHERE dc.content_chunk ILIKE %s
                    ORDER BY dc.chunk_index LIMIT 5""",
                params + [f"%{query}%"]
            )
            rows = cur.fetchall()
            fallback_results = [{
                "doc_name": r[1], "content_chunk": r[2][:500],
                "chunk_index": r[3], "similarity": 0.5, "distance": 0.5
            } for r in rows]
            conn.close()

            return jsonify({"results": fallback_results, "query": query, "fallback": True})
        except Exception as e2:
            return jsonify({"error": f"ERRO RAG: {e} | Fallback: {e2}"}), 500


LLM_API_URL = os.environ.get("LLM_API_URL", "http://192.168.25.8:1234/v1/chat/completions")

@app.route('/api/chat', methods=['POST'])
def chat():
    """Chat com contexto RAG — busca chunks similares, monta prompt e chama LLM local."""
    data = request.get_json()
    message = data.get('message', '')
    area_id = data.get('area_id')

    if not message:
        return jsonify({"error": "Campo 'message' é obrigatório"}), 400

    try:
        # Busca chunks relevantes via RAG
        context_chunks = search_similar(message, area_id=area_id, top_k=50)

        # Enrich com nomes de documentos
        conn = get_db_connection()
        doc_names = {}
        if conn and context_chunks:
            cur = conn.cursor()
            doc_ids = list(set(r["doc_id"] for r in context_chunks))
            if doc_ids:
                placeholders = ",".join(["%s"] * len(doc_ids))
                cur.execute(
                    f"SELECT id, name FROM documents WHERE id IN ({placeholders})",
                    doc_ids
                )
                doc_names = {row[0]: row[1] for row in cur.fetchall()}

        context_sources = []
        full_context_text = ""
        for chunk in context_chunks:
            similarity = round(1.0 - chunk["distance"], 4)
            context_sources.append({
                "source": doc_names.get(chunk["doc_id"], f"Doc #{chunk['doc_id']}"),
                "text": chunk["content_chunk"][:600],
                "similarity": similarity,
                "chunk_index": chunk["chunk_index"]
            })
            full_context_text += chunk["content_chunk"] + "\n\n"

        if conn:
            conn.close()

        # Monta prompt com contexto RAG
        system_prompt = (
            "Você é um tutor inteligente especializado em educação e análise técnica. "
            "Responda as perguntas do usuário usando o contexto fornecido abaixo COMO REFERÊNCIA, "
            "mas também pode usar seu conhecimento geral para complementar a resposta. "
            "Se o contexto RAG não cobrir todos os aspectos da pergunta, complete com seu conhecimento prévio. "
            "Cite quando algo vem do contexto vs conhecimento geral. "
            "Responda em português de forma clara e didática."
        )

        user_prompt = f"""Contexto do documento:
{'=' * 60}
{full_context_text}
{'=' * 60}

Pergunta: {message}"""

        # Chama LLM local (timeout=60s, max_tokens=30k)
        llm_response = _http_requests.post(
            LLM_API_URL,
            json={
                "model": "auto",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 30000
            },
            timeout=6000
        )
        llm_response.raise_for_status()
        llm_data = llm_response.json()
        response_text = llm_data["choices"][0]["message"]["content"]

        return jsonify({
            "response": response_text,
            "context_sources": context_sources,
            "area_id": area_id,
            "message": message
        })

    except Exception as e:
        print(f"ERRO chat RAG: {e}")
        return jsonify({
            "response": f"Erro ao processar consulta: {str(e)}",
            "context_sources": [],
            "area_id": area_id,
            "error": str(e)
        })


# ---- Admin endpoints ----

@app.route('/admin/areas', methods=['GET'])
def admin_get_areas():
    """Lista todas as áreas com contagem de documentos."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível", "total": 0, "areas": []}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, slug FROM areas WHERE status = 'active' ORDER BY name")
        rows = cur.fetchall()
        areas = []
        for r in rows:
            cur.execute("SELECT count(*) FROM documents WHERE area_id = %s", (r[0],))
            doc_count = cur.fetchone()[0]
            areas.append({"id": r[0], "name": r[1], "slug": r[2], "doc_count": doc_count})
        conn.close()
        return jsonify({"total": len(areas), "areas": areas})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/areas', methods=['POST'])
def admin_create_area():
    """Cria uma nova área temática."""
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"error": "Nome é obrigatório"}), 400
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        slug = name.lower().replace(' ', '-').replace('/', '-')
        vector_ref = f"area_{slug}_v1"
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO areas (name, slug, vector_ref, status) VALUES (%s, %s, %s, 'active') RETURNING id",
            (name, slug, vector_ref)
        )
        area_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": area_id, "name": name, "slug": slug}), 201
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/areas/<int:area_id>', methods=['PATCH'])
def admin_update_area(area_id):
    """Atualiza nome de uma área."""
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"error": "Nome é obrigatório"}), 400
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        slug = name.lower().replace(' ', '-').replace('/', '-')
        cur = conn.cursor()
        cur.execute("UPDATE areas SET name = %s, slug = %s WHERE id = %s", (name, slug, area_id))
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Área não encontrada"}), 404
        conn.close()
        return jsonify({"id": area_id, "name": name, "slug": slug})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/areas/<int:area_id>', methods=['DELETE'])
def admin_delete_area(area_id):
    """Desativa uma área (soft delete)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("UPDATE areas SET status = 'inactive' WHERE id = %s", (area_id,))
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Área não encontrada"}), 404
        conn.close()
        return jsonify({"message": f"Área {area_id} desativada"})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/documents', methods=['GET'])
def admin_get_documents():
    """Lista todos os documentos com nome da área."""
    area_id = request.args.get('area_id')
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível", "total": 0, "documents": []}), 500
    try:
        cur = conn.cursor()
        if area_id:
            cur.execute(
                """SELECT d.id, d.name, a.name as area_name, d.is_external_link, d.url,
                          d.processing_status, d.chunk_count
                   FROM documents d JOIN areas a ON a.id = d.area_id
                   WHERE d.area_id = %s ORDER BY d.upload_date DESC""",
                (area_id,)
            )
        else:
            cur.execute(
                """SELECT d.id, d.name, a.name as area_name, d.is_external_link, d.url,
                          d.processing_status, d.chunk_count
                   FROM documents d JOIN areas a ON a.id = d.area_id
                   ORDER BY d.upload_date DESC"""
            )
        rows = cur.fetchall()
        docs = []
        for r in rows:
            docs.append({
                "id": r[0], "name": r[1], "area_name": r[2],
                "type": "link" if r[3] else "file", "url": r[4] or "",
                "processing_status": r[5] or "pending",
                "chunk_count": r[6] or 0
            })
        conn.close()
        return jsonify({"total": len(docs), "documents": docs})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/documents/<int:doc_id>', methods=['DELETE'])
def admin_delete_document(doc_id):
    """Exclui um documento (hard delete)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        # Remove chunks primeiro (FK constraint)
        cur.execute("DELETE FROM document_chunks WHERE doc_id = %s", (doc_id,))
        cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Documento não encontrado"}), 404
        conn.close()
        return jsonify({"message": f"Documento {doc_id} excluído"})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/documents/<int:doc_id>/move', methods=['PATCH'])
def admin_move_document(doc_id):
    """Move um documento para outra área."""
    data = request.get_json()
    new_area_id = data.get('area_id')
    if not new_area_id:
        return jsonify({"error": "area_id é obrigatório"}), 400
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        # Verifica se a área existe
        cur.execute("SELECT id, name FROM areas WHERE id = %s AND status = 'active'", (new_area_id,))
        area_row = cur.fetchone()
        if not area_row:
            conn.close()
            return jsonify({"error": "Área não encontrada ou inativa"}), 404

        # Atualiza a área do documento
        cur.execute(
            "UPDATE documents SET area_id = %s WHERE id = %s",
            (new_area_id, doc_id)
        )
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Documento não encontrado"}), 404

        # Atualiza area_id nos chunks vinculados
        cur.execute(
            "UPDATE document_chunks SET area_id = %s WHERE doc_id = %s",
            (new_area_id, doc_id)
        )
        conn.commit()
        conn.close()
        return jsonify({"message": f"Documento {doc_id} movido para área '{area_row[1]}'"})
    except Exception as e:
        if conn: conn.rollback()
        conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/documents/<int:doc_id>/reprocess', methods=['POST'])
def admin_reprocess_document(doc_id):
    """Reprocessa RAG de um documento."""
    result = process_document(doc_id)
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "Erro desconhecido")}), 500
    return jsonify({
        "message": f"Documento {doc_id} reprocessado",
        "chunks_created": result.get("chunks_created", 0),
        "saved_count": result.get("saved_count", 0)
    })


@app.route('/api/upload', methods=['POST'])
def upload_file():
    """Upload de arquivo (PDF ou TXT) com extração automática e RAG."""
    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    
    file = request.files['file']
    area_id = request.form.get('area_id')
    name = request.form.get('name', file.filename)
    
    if not area_id or not file.filename:
        return jsonify({"error": "area_id e arquivo são obrigatórios"}), 400
    
    # Lê bytes do arquivo
    file_bytes = file.read()
    ext = os.path.splitext(file.filename)[1].lower()
    
    # Extrai texto conforme tipo
    content_text = None
    if ext == '.pdf':
        content_text = extract_pdf_text(file_bytes)
    elif ext in ('.txt', '.text'):
        try:
            content_text = file_bytes.decode('utf-8', errors='replace').strip()
        except Exception as e:
            return jsonify({"error": f"Erro ao ler arquivo TXT: {e}"}), 500
    else:
        return jsonify({"error": "Formato não suportado. Use PDF ou TXT."}), 400
    
    if not content_text or len(content_text) < 20:
        return jsonify({"error": f"Não foi possível extrair texto do arquivo (extensão: {ext}). O PDF pode conter apenas imagens."}), 500
    
    # Salva no banco
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO documents (area_id, name, is_external_link, status, processing_status, content_text, upload_date)
               VALUES (%s, %s, false, 'active', 'pending', %s, NOW()) RETURNING id""",
            (int(area_id), name, content_text)
        )
        doc_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    
    # Processa RAG (chunk + embed)
    result = process_document(doc_id)
    
    return jsonify({
        "id": doc_id,
        "message": f"Arquivo '{name}' processado com sucesso",
        "text_extracted_len": len(content_text),
        "rag_result": result
    }), 201


@app.route('/api/health', methods=['GET'])
def health_check():
    """Verifica saúde do sistema."""
    conn = get_db_connection()
    db_ok = conn is not None
    area_count = 0
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM areas WHERE status = 'active'")
            area_count = cur.fetchone()[0]
            conn.close()
        except Exception:
            pass

    return jsonify({
        "ok": db_ok,
        "service": "ai-tutor-api",
        "port": 5001,
        "db_connected": db_ok,
        "area_count": area_count
    })


# Serve frontend static files
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'frontend')

@app.route('/<path:filename>')
def serve_frontend(filename):
    """Serve HTML/frontend files."""
    if filename.endswith(('.html', '.css', '.js')):
        return send_from_directory(FRONTEND_DIR, filename)
    # Fallback: let other routes handle it
    return jsonify({"error": "Not found"}), 404


if __name__ == '__main__':
    migrate_if_needed()
    print("API Server rodando em http://localhost:5001 (RAG integrado)")
    app.run(host='0.0.0.0', port=5001, debug=False)
