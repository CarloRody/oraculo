"""Admin Server — API de administração do AI Tutor SaaS (todas as tabelas).
Sem autenticação. Roda na porta 5002.
"""

from flask import Flask, jsonify, request
import psycopg2
import json
from flask_cors import CORS

# Importar RAG engine para reprocessamento
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from rag_engine import process_document
from config import DB_CONFIG

app = Flask(__name__)
CORS(app)


# ---------------------------------------------------------------------------
# Conexão DB (config.yaml, na raiz do monorepo)
# ---------------------------------------------------------------------------


def get_db():
    """Retorna conexão PostgreSQL ou None."""
    try:
        return psycopg2.connect(**DB_CONFIG)
    except Exception as e:
        print(f"ERRO DB admin: {e}")
        return None


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------
@app.route("/api/health", methods=["GET"])
def health():
    """Confirma que o admin server está rodando e conectado ao banco."""
    conn = get_db()
    if not conn:
        return jsonify({"ok": False, "db_connected": False}), 500

    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM areas")
        area_count = cur.fetchone()[0]
        conn.close()
        return jsonify({
            "ok": True,
            "service": "admin-server",
            "port": 5002,
            "db_connected": True,
            "area_count": area_count,
        })
    except Exception as e:
        if conn:
            conn.close()
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# CRUD Documentos + Chunks (documents + document_chunks)
# ---------------------------------------------------------------------------

@app.route("/admin/documents", methods=["GET"])
def list_documents():
    """Lista todos os documentos com filtros opcionais."""
    area_id = request.args.get("area_id")
    status = request.args.get("status")  # active, stale, invalid
    is_external = request.args.get("is_external_link")  # true/false
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)

    conn = get_db()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco"}), 500

    try:
        cur = conn.cursor()
        conditions = []
        params = []

        if area_id:
            conditions.append("d.area_id = %s")
            params.append(area_id)
        if status:
            conditions.append("d.status = %s")
            params.append(status)
        if is_external is not None:
            val = is_external.lower() == "true"
            conditions.append(f"d.is_external_link = {val}")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Contagem total
        cur.execute(f"SELECT COUNT(*) FROM documents d {where}", params)
        total = cur.fetchone()[0]

        # Dados
        sql = f"""SELECT d.id, d.area_id,
                         (SELECT a.name FROM areas a WHERE a.id = d.area_id) as area_name,
                         d.name, d.url, d.is_external_link,
                         d.status, d.processing_status, d.chunk_count,
                         d.last_checked_at, d.upload_date
                   FROM documents d {where}
                   ORDER BY d.upload_date DESC
                   LIMIT %s OFFSET %s"""
        cur.execute(sql, params + [limit, offset])
        rows = cur.fetchall()

        docs = []
        for r in rows:
            docs.append({
                "id": r[0],
                "area_id": r[1],
                "area_name": r[2],
                "name": r[3],
                "url": r[4] or "",
                "is_external_link": r[5],
                "status": r[6],
                "processing_status": r[7] or "pending",
                "chunk_count": r[8] or 0,
                "last_checked_at": r[9].isoformat() if r[9] else None,
                "upload_date": r[10].isoformat() if r[10] else None,
            })

        conn.close()
        return jsonify({"documents": docs, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/admin/documents", methods=["POST"])
def create_document():
    """Cria um novo documento (sem processar RAG automaticamente — admin decide quando)."""
    data = request.get_json()
    area_id = data.get("area_id")
    name = data.get("name", "Documento sem nome")
    url = data.get("url")
    is_external = data.get("is_external_link", False)
    content_text = data.get("content_text", "")

    if not area_id:
        return jsonify({"error": "area_id é obrigatório"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco"}), 500

    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO documents (area_id, name, url, is_external_link, content_text,
                                      status, processing_status, last_checked_at, upload_date)
               VALUES (%s, %s, %s, %s, %s, 'active', 'pending', NOW(), NOW())
               RETURNING id""",
            (area_id, name, url if is_external else None, is_external, content_text or None),
        )
        doc_id = cur.fetchone()[0]
        conn.commit()
        conn.close()

        return jsonify({"id": doc_id, "message": "Documento criado com sucesso"}), 201
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/admin/documents/<int:doc_id>", methods=["PATCH"])
def update_document(doc_id):
    """Edita campos de um documento existente."""
    data = request.get_json()

    conn = get_db()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco"}), 500

    try:
        cur = conn.cursor()

        # Verifica existência
        cur.execute("SELECT id FROM documents WHERE id = %s", (doc_id,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"error": f"Documento {doc_id} não encontrado"}), 404

        # Build dynamic UPDATE from provided fields
        setClauses = []
        params = []

        updatable_fields = {
            "name": "name",
            "area_id": "area_id",
            "url": "url",
            "is_external_link": "is_external_link",
            "content_text": "content_text",
            "status": "status",
            "processing_status": "processing_status",
        }

        for field, col in updatable_fields.items():
            if field in data:
                setClauses.append(f"{col} = %s")
                params.append(data[field])

        if not setClauses:
            conn.close()
            return jsonify({"error": "Nenhum campo válido para atualizar"}), 400

        # Also update area_id in chunks if changed
        if "area_id" in data:
            setClauses.append("last_checked_at = NOW()")

        params.append(doc_id)
        sql = f"UPDATE documents SET {', '.join(setClauses)} WHERE id = %s"
        cur.execute(sql, params)
        conn.commit()

        if "area_id" in data:
            # Update area_id in chunks too
            cur.execute(
                "UPDATE document_chunks SET area_id = %s WHERE doc_id = %s",
                (data["area_id"], doc_id),
            )
            conn.commit()

        conn.close()
        return jsonify({"message": f"Documento {doc_id} atualizado com sucesso"})
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/admin/documents/<int:doc_id>", methods=["DELETE"])
def delete_document(doc_id):
    """Remove um documento e seus chunks em cascata."""
    conn = get_db()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco"}), 500

    try:
        cur = conn.cursor()

        # Verifica existência
        cur.execute("SELECT id FROM documents WHERE id = %s", (doc_id,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"error": f"Documento {doc_id} não encontrado"}), 404

        # Remove chunks primeiro (FK constraint)
        cur.execute(
            "DELETE FROM document_chunks WHERE doc_id = %s RETURNING COUNT(*)",
            (doc_id,),
        )
        chunks_deleted = cur.fetchone()[0]

        # Remove documento
        cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
        conn.commit()
        conn.close()

        return jsonify({
            "message": f"Documento {doc_id} removido",
            "chunks_deleted": chunks_deleted,
        })
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/admin/documents/<int:doc_id>/chunks", methods=["GET"])
def list_chunks(doc_id):
    """Lista todos os chunks de um documento com embeddings."""
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)

    conn = get_db()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco"}), 500

    try:
        cur = conn.cursor()

        # Verifica existência do documento
        cur.execute("SELECT id FROM documents WHERE id = %s", (doc_id,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"error": f"Documento {doc_id} não encontrado"}), 404

        # Contagem total
        cur.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = %s", (doc_id,)
        )
        total = cur.fetchone()[0]

        # Dados
        try:
            cur.execute(
                """SELECT id, content_chunk, chunk_index, embedding_vector, created_at
                   FROM document_chunks WHERE doc_id = %s
                   ORDER BY chunk_index
                   LIMIT %s OFFSET %s""",
                (doc_id, limit, offset),
            )
        except Exception:
            # Fallback se 'created_at' não existe na tabela
            cur.execute(
                """SELECT id, content_chunk, chunk_index, embedding_vector
                   FROM document_chunks WHERE doc_id = %s
                   ORDER BY chunk_index
                   LIMIT %s OFFSET %s""",
                (doc_id, limit, offset),
            )

        rows = cur.fetchall()

        chunks = []
        for r in rows:
            # Resumir embedding_vector (pode ser enorme)
            emb_preview = ""
            if r[3]:
                try:
                    vec = json.loads(r[3])
                    emb_preview = f"[{len(vec)} dims] {vec[:3]}..."
                except Exception:
                    emb_preview = "[erro ao decodificar]"

            created_at = r[4].isoformat() if len(r) > 4 and r[4] else None

            chunks.append({
                "id": r[0],
                "content_chunk": r[1],
                "chunk_index": r[2],
                "embedding_vector": emb_preview,
                "created_at": created_at,
            })

        conn.close()
        return jsonify({"chunks": chunks, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/admin/documents/<int:doc_id>/reprocess", methods=["POST"])
def reprocess_document(doc_id):
    """Reprocessa RAG de um documento (fetch + chunk + embed + salva)."""
    result = process_document(doc_id)

    if not result.get("ok"):
        return jsonify({"error": result.get("error", "Erro desconhecido")}), 500

    # Atualiza status no documento
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            chunk_count = result.get("saved_count", result.get("chunks_created", 0))
            cur.execute(
                """UPDATE documents SET processing_status = 'complete',
                   chunk_count = %s, last_checked_at = NOW() WHERE id = %s""",
                (chunk_count, doc_id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            if conn:
                conn.rollback()
                conn.close()

    return jsonify({
        "message": f"Documento {doc_id} processado com sucesso",
        "chunks_created": result.get("chunks_created", 0),
        "saved_count": result.get("saved_count", 0),
    })


# ---------------------------------------------------------------------------
# Erros globais
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint não encontrado"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Erro interno do servidor"}), 500


# ---------------------------------------------------------------------------
# CRUD Áreas (areas)
# ---------------------------------------------------------------------------

@app.route("/admin/areas", methods=["GET"])
def list_areas():
    """Lista todas as áreas (incluindo draft e archived)."""
    status = request.args.get("status")  # active, draft, archived
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)

    conn = get_db()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco"}), 500

    try:
        cur = conn.cursor()

        conditions = []
        params = []
        if status:
            conditions.append("status = %s")
            params.append(status)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Contagem total
        cur.execute(f"SELECT COUNT(*) FROM areas {where}", params)
        total = cur.fetchone()[0]

        # Dados
        sql = f"""SELECT a.id, a.name, a.slug, a.vector_ref, a.status,
                         (SELECT COUNT(*) FROM documents d WHERE d.area_id = a.id) as doc_count
                   FROM areas a {where}
                   ORDER BY a.name
                   LIMIT %s OFFSET %s"""
        cur.execute(sql, params + [limit, offset])
        rows = cur.fetchall()

        areas_list = []
        for r in rows:
            areas_list.append({
                "id": r[0],
                "name": r[1],
                "slug": r[2],
                "vector_ref": r[3] or "",
                "status": r[4],
                "doc_count": r[5],
            })

        conn.close()
        return jsonify({"areas": areas_list, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/admin/areas", methods=["POST"])
def create_area():
    """Cria uma nova área."""
    data = request.get_json()
    name = data.get("name")
    slug = data.get("slug")
    vector_ref = data.get("vector_ref", "")
    status = data.get("status", "active")  # active, draft, archived

    if not name or not slug:
        return jsonify({"error": "name e slug são obrigatórios"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco"}), 500

    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO areas (name, slug, vector_ref, status)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (name, slug, vector_ref or None, status),
        )
        area_id = cur.fetchone()[0]
        conn.commit()
        conn.close()

        return jsonify({"id": area_id, "message": f"Área '{name}' criada com sucesso"}), 201
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        err_str = str(e)
        if "duplicate key" in err_str and "slug" in err_str.lower():
            return jsonify({"error": f"Slug '{slug}' já existe"}), 409
        return jsonify({"error": err_str}), 500


@app.route("/admin/areas/<int:area_id>", methods=["PATCH"])
def update_area(area_id):
    """Edita campos de uma área existente."""
    data = request.get_json()

    conn = get_db()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco"}), 500

    try:
        cur = conn.cursor()

        # Verifica existência
        cur.execute("SELECT id FROM areas WHERE id = %s", (area_id,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"error": f"Área {area_id} não encontrada"}), 404

        setClauses = []
        params = []

        updatable_fields = {
            "name": "name",
            "slug": "slug",
            "vector_ref": "vector_ref",
            "status": "status",
        }

        for field, col in updatable_fields.items():
            if field in data:
                setClauses.append(f"{col} = %s")
                params.append(data[field])

        if not setClauses:
            conn.close()
            return jsonify({"error": "Nenhum campo válido para atualizar"}), 400

        params.append(area_id)
        sql = f"UPDATE areas SET {', '.join(setClauses)} WHERE id = %s"
        cur.execute(sql, params)
        conn.commit()
        conn.close()

        return jsonify({"message": f"Área {area_id} atualizada com sucesso"})
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        err_str = str(e)
        if "duplicate key" in err_str and "slug" in err_str.lower():
            return jsonify({"error": f"Slug '{data.get('slug')}' já existe"}), 409
        return jsonify({"error": err_str}), 500


@app.route("/admin/areas/<int:area_id>", methods=["DELETE"])
def delete_area(area_id):
    """Remove uma área (verifica se tem documentos antes)."""
    conn = get_db()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco"}), 500

    try:
        cur = conn.cursor()

        # Verifica existência
        cur.execute("SELECT id, name FROM areas WHERE id = %s", (area_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": f"Área {area_id} não encontrada"}), 404

        area_name = row[1]

        # Verifica se tem documentos vinculados
        cur.execute("SELECT COUNT(*) FROM documents WHERE area_id = %s", (area_id,))
        doc_count = cur.fetchone()[0]

        if doc_count > 0:
            conn.close()
            return jsonify({
                "error": f"Nao e possivel remover area '{area_name}' — existem {doc_count} documento(s) vinculados. Remova-os primeiro.",
                "document_count": doc_count,
            }), 409

        # Remove a área
        cur.execute("DELETE FROM areas WHERE id = %s", (area_id,))
        conn.commit()
        conn.close()

        return jsonify({"message": f"Area '{area_name}' removida com sucesso"})
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Admin Server rodando em http://0.0.0.0:5002")
    app.run(host="0.0.0.0", port=5002, debug=False)
