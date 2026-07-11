from flask import Flask, jsonify, request, send_from_directory
import psycopg2
import json
import os
import secrets
import subprocess
import copy
from datetime import date
import requests as _http_requests
from flask_cors import CORS

# Importar RAG engine (pipeline completo: fetch → chunk com overlap → embed → salva)
import sys, os as _os_module
sys.path.insert(0, _os_module.path.join(_os_module.path.dirname(__file__), '..'))
from rag_engine import process_document, search_similar, get_model, extract_pdf_text
from migrations import migrate_if_needed
from config import CONFIG, DB_CONFIG, save_config

app = Flask(__name__)
CORS(app)  # Permite que o frontend acesse de qualquer origem local


def get_db_connection():
    """Conecta ao banco de dados PostgreSQL (via config.yaml — socket Unix por padrão)."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
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
    """Retorna documentos de uma área ou todos. ?limit= restringe aos N mais recentes."""
    area_id = request.args.get('area_id')
    try:
        limit = min(int(request.args.get('limit')), 200) if request.args.get('limit') else None
    except (TypeError, ValueError):
        limit = None

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Não foi possível conectar ao banco de dados"}), 500

    try:
        cur = conn.cursor()
        limit_clause = " LIMIT %s" if limit else ""
        if area_id:
            params = (area_id, limit) if limit else (area_id,)
            cur.execute(
                f"""SELECT id, name, is_external_link, url, processing_status, chunk_count, fetch_mode
                   FROM documents WHERE area_id = %s ORDER BY upload_date DESC{limit_clause}""",
                params
            )
        else:
            params = (limit,) if limit else ()
            cur.execute(
                f"""SELECT id, name, is_external_link, url, processing_status, chunk_count, fetch_mode
                   FROM documents ORDER BY upload_date DESC{limit_clause}""",
                params
            )

        rows = cur.fetchall()
        docs = []
        for r in rows:
            doc_type = "link" if r[2] else "file"
            docs.append({
                "id": r[0], "name": r[1], "type": doc_type, "url": r[3] or "",
                "processing_status": r[4] or "pending",
                "chunk_count": r[5] or 0,
                "fetch_mode": r[6] or "http"
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
    fetch_mode = data.get('fetch_mode') or 'http'

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Não foi possível conectar ao banco de dados"}), 500

    try:
        cur = conn.cursor()

        # INSERT básico com RETURNING
        cur.execute(
            """INSERT INTO documents (area_id, name, url, is_external_link, status, processing_status, last_checked_at, upload_date, fetch_mode)
               VALUES (%s, %s, %s, %s, 'active', 'pending', NOW(), NOW(), %s) RETURNING id""",
            (area_id, data.get('name', 'Documento sem nome'), url if url else None, is_external, fetch_mode)
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
    area_ids = resolve_area_ids(data)
    try:
        top_k = max(1, min(int(data.get('top_k') or 8), 20))
    except (TypeError, ValueError):
        top_k = 8

    if not query:
        return jsonify({"error": "Campo 'query' é obrigatório"}), 400

    try:
        results = search_similar(query, area_ids=area_ids, top_k=top_k)

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
            area_clause = "AND d.area_id = ANY(%s)" if area_ids else ""
            params = [list(area_ids)] if area_ids else []

            cur.execute(
                f"""SELECT dc.id, d.name as doc_name, dc.content_chunk, dc.chunk_index
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.doc_id
                    WHERE dc.content_chunk ILIKE %s
                    {area_clause}
                    ORDER BY dc.chunk_index LIMIT 5""",
                [f"%{query}%"] + params
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


LLM_CONFIG = CONFIG["llm"]
QUOTA_ENFORCEMENT = CONFIG["tutor"]["quota_enforcement"]  # 'warn' | 'block' | 'off'

_tiktoken_encoder = None


def count_tokens(text):
    """Estimativa de tokens via tiktoken — fallback usado só quando o gateway LLM não
    retorna 'usage' na resposta. Aproximado: não é o tokenizer real do modelo local."""
    global _tiktoken_encoder
    try:
        if _tiktoken_encoder is None:
            import tiktoken
            _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
        return len(_tiktoken_encoder.encode(text or ""))
    except Exception as e:
        print(f"ERRO count_tokens (tiktoken): {e}")
        return 0


def resolve_user_from_request():
    """Resolve o user_id do cliente a partir do header X-Oraculo-Key. None = anônimo."""
    api_key = request.headers.get('X-Oraculo-Key')
    if not api_key:
        return None
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE api_key = %s", (api_key,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        if conn: conn.close()
        print(f"ERRO resolve_user_from_request: {e}")
        return None


def _area_name(area_id):
    """Nome de uma área pelo id, para mensagens de cota e cabeçalhos de contexto."""
    if not area_id:
        return "Desconhecida"
    conn = get_db_connection()
    if not conn:
        return f"Área #{area_id}"
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM areas WHERE id = %s", (area_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else f"Área #{area_id}"
    except Exception:
        if conn: conn.close()
        return f"Área #{area_id}"


def resolve_area_ids(data):
    """Lê 'area_ids' (lista, formato atual) do corpo da requisição; aceita
    'area_id' (escalar, formato antigo) como fallback. None/[] = todas as áreas."""
    area_ids = data.get('area_ids')
    if area_ids:
        return [int(a) for a in area_ids]
    area_id = data.get('area_id')
    return [int(area_id)] if area_id else None


def get_quota_status(user_id, area_id):
    """Cota mensal, uso do mês corrente e preço configurado para um cliente+área,
    resolvidos através do plano de assinatura atual do cliente (vínculo ao vivo —
    editar o plano já reflete aqui, sem precisar reatribuir nada).
    None se o cliente não tem plano, ou o plano não define nada pra essa área
    (sem cota = sem checagem)."""
    if not user_id or not area_id:
        return None
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT pap.monthly_token_quota, pap.price_per_1k_tokens
               FROM users u JOIN plan_area_pricing pap ON pap.plan_id = u.plan_id AND pap.area_id = %s
               WHERE u.id = %s""",
            (area_id, user_id)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        quota, price = row[0], row[1]
        cur.execute(
            """SELECT COALESCE(SUM(tokens_input + tokens_output), 0) FROM usage_logs
               WHERE user_id = %s AND area_id = %s AND timestamp >= date_trunc('month', now())""",
            (user_id, area_id)
        )
        used = cur.fetchone()[0]
        conn.close()
        return {
            "quota": quota,
            "used": used,
            "remaining": (quota - used) if quota is not None else None,
            "price_per_1k_tokens": float(price) if price is not None else None
        }
    except Exception as e:
        if conn: conn.close()
        print(f"ERRO get_quota_status: {e}")
        return None


def log_chat_message(user_id, area_ids, message, response_text, tokens_input, tokens_output):
    """Grava a sessão + as duas mensagens (pergunta/resposta) uma vez só,
    independente de quantas áreas a pergunta usou. A sessão fica associada à
    primeira área da lista só para fins de exibição de histórico.
    Nunca levanta — falha aqui não deve quebrar o chat. Retorna session_id ou None."""
    if not user_id:
        return None
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        primary_area_id = area_ids[0] if area_ids else None
        cur.execute(
            """SELECT id FROM sessions WHERE user_id = %s AND area_id = %s
               AND created_at::date = CURRENT_DATE ORDER BY created_at DESC LIMIT 1""",
            (user_id, primary_area_id)
        )
        row = cur.fetchone()
        session_id = row[0] if row else None
        if session_id is None:
            cur.execute(
                "INSERT INTO sessions (user_id, area_id, title) VALUES (%s, %s, %s) RETURNING id",
                (user_id, primary_area_id, message[:60])
            )
            session_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO messages (session_id, role, content, token_count) VALUES (%s, 'user', %s, %s)",
            (session_id, message, tokens_input)
        )
        cur.execute(
            "INSERT INTO messages (session_id, role, content, token_count) VALUES (%s, 'assistant', %s, %s)",
            (session_id, response_text, tokens_output)
        )
        conn.commit()
        conn.close()
        return session_id
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        print(f"ERRO ao gravar sessions/messages: {e}")
        return None


def log_area_usage(user_id, session_id, area_id, tokens_input, tokens_output):
    """Grava uma linha de usage_logs para uma área, com a fatia de tokens já
    calculada (rateio proporcional é feito pelo chamador). Nunca levanta."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usage_logs (user_id, session_id, area_id, tokens_input, tokens_output) VALUES (%s, %s, %s, %s, %s)",
            (user_id, session_id, area_id, tokens_input, tokens_output)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        print(f"ERRO ao gravar usage_logs: {e}")


def split_tokens_by_area(area_ids, chunk_counts_by_area, tokens_input, tokens_output):
    """Rateia tokens_input/tokens_output entre as áreas, proporcional a quantos
    chunks cada área contribuiu para o contexto. Sem chunks em nenhuma área
    (contexto vazio), rateia igualmente. A última área recebe o resto da
    divisão inteira, para a soma das partes fechar com o total exato."""
    n = len(area_ids)
    total_chunks = sum(chunk_counts_by_area.get(aid, 0) for aid in area_ids)

    shares = []
    if total_chunks > 0:
        for aid in area_ids:
            shares.append(chunk_counts_by_area.get(aid, 0) / total_chunks)
    else:
        shares = [1.0 / n] * n

    result = []
    used_input = used_output = 0
    for i, (aid, share) in enumerate(zip(area_ids, shares)):
        if i == n - 1:
            area_input = tokens_input - used_input
            area_output = tokens_output - used_output
        else:
            area_input = round(tokens_input * share)
            area_output = round(tokens_output * share)
            used_input += area_input
            used_output += area_output
        result.append((aid, area_input, area_output))
    return result


@app.route('/api/chat', methods=['POST'])
def chat():
    """Chat com contexto RAG — busca chunks similares, monta prompt e chama LLM local.
    Aceita `area_ids` (lista) para combinar várias áreas numa mesma pergunta;
    None/[] busca em todas as áreas."""
    data = request.get_json()
    message = data.get('message', '')
    requested_area_ids = resolve_area_ids(data)
    user_id = resolve_user_from_request()

    if not message:
        return jsonify({"error": "Campo 'message' é obrigatório"}), 400

    try:
        # Busca chunks relevantes via RAG (uma ou várias áreas, ou todas se None)
        context_chunks = search_similar(message, area_ids=requested_area_ids, top_k=50)

        # Áreas que de fato contribuíram trechos para o contexto — é isso que
        # define a cota/billing, não o que foi pedido (uma área pedida sem
        # nenhum trecho relevante não deve ser cobrada nem checada).
        chunk_counts_by_area = {}
        for chunk in context_chunks:
            aid = chunk["area_id"]
            chunk_counts_by_area[aid] = chunk_counts_by_area.get(aid, 0) + 1
        billing_area_ids = list(chunk_counts_by_area.keys()) or (requested_area_ids or [])

        # Checagem de cota mensal, antes de gastar uma chamada de LLM
        quota_warning = None
        over_quota_names = []
        for aid in billing_area_ids:
            quota_status = get_quota_status(user_id, aid)
            if quota_status and quota_status["quota"] is not None and quota_status["used"] >= quota_status["quota"]:
                over_quota_names.append(_area_name(aid))
        if over_quota_names:
            if QUOTA_ENFORCEMENT == "block":
                return jsonify({
                    "error": f"Limite mensal de tokens excedido para: {', '.join(over_quota_names)}."
                }), 429
            elif QUOTA_ENFORCEMENT == "warn":
                quota_warning = f"Uso de tokens acima do limite mensal contratado para: {', '.join(over_quota_names)}."

        # Enrich com nomes de documentos e áreas
        conn = get_db_connection()
        doc_names = {}
        area_names = {}
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
            for aid in chunk_counts_by_area:
                area_names[aid] = _area_name(aid)

        context_sources = []
        # Agrupa o contexto por área, com um cabeçalho por seção — ajuda o
        # modelo a não misturar regras de domínios diferentes numa pergunta
        # que cruza áreas (ex: layout técnico de XML + legislação fiscal).
        chunks_by_area = {}
        for chunk in context_chunks:
            chunks_by_area.setdefault(chunk["area_id"], []).append(chunk)

        context_sections = []
        for aid, chunks in chunks_by_area.items():
            section_lines = [f"=== Contexto: {area_names.get(aid, f'Área #{aid}')} ==="]
            for chunk in chunks:
                similarity = round(1.0 - chunk["distance"], 4)
                context_sources.append({
                    "source": doc_names.get(chunk["doc_id"], f"Doc #{chunk['doc_id']}"),
                    "area_id": aid,
                    "area_name": area_names.get(aid, f"Área #{aid}"),
                    "text": chunk["content_chunk"][:600],
                    "similarity": similarity,
                    "chunk_index": chunk["chunk_index"]
                })
                section_lines.append(chunk["content_chunk"])
            context_sections.append("\n\n".join(section_lines))

        if conn:
            conn.close()

        full_context_text = "\n\n".join(context_sections)

        # Monta prompt com contexto RAG
        system_prompt = (
            "Você é um tutor inteligente especializado em educação e análise técnica. "
            "Responda as perguntas do usuário usando o contexto fornecido abaixo COMO REFERÊNCIA, "
            "mas também pode usar seu conhecimento geral para complementar a resposta. "
            "O contexto pode vir de mais de uma área de conhecimento, cada uma em sua própria seção — "
            "não misture regras de áreas diferentes ao responder. "
            "Se o contexto RAG não cobrir todos os aspectos da pergunta, complete com seu conhecimento prévio. "
            "Cite quando algo vem do contexto vs conhecimento geral. "
            "Responda em português de forma clara e didática."
        )

        user_prompt = f"""Contexto do documento:
{'=' * 60}
{full_context_text}
{'=' * 60}

Pergunta: {message}"""

        # Chama LLM (provedor/modelo/token/parâmetros vêm de config.yaml)
        llm_headers = {"Authorization": f"Bearer {LLM_CONFIG['api_key']}"} if LLM_CONFIG.get("api_key") else {}
        llm_response = _http_requests.post(
            LLM_CONFIG["base_url"],
            json={
                "model": LLM_CONFIG.get("model", "auto"),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": LLM_CONFIG.get("temperature", 0.7),
                "max_tokens": LLM_CONFIG.get("max_tokens", 6000)
            },
            headers=llm_headers,
            timeout=LLM_CONFIG.get("timeout_seconds", 600)
        )
        llm_response.raise_for_status()
        llm_data = llm_response.json()
        response_text = llm_data["choices"][0]["message"]["content"]

        # Contagem de tokens: usa o "usage" do gateway (exato) se disponível,
        # senão estima via tiktoken (aproximado — ver count_tokens)
        usage = llm_data.get("usage") or {}
        tokens_input = usage.get("prompt_tokens")
        tokens_output = usage.get("completion_tokens")
        if tokens_input is None or tokens_output is None:
            tokens_input = count_tokens(system_prompt + user_prompt)
            tokens_output = count_tokens(response_text)

        session_id = log_chat_message(user_id, billing_area_ids, message, response_text, tokens_input, tokens_output)
        if billing_area_ids:
            for aid, area_input, area_output in split_tokens_by_area(billing_area_ids, chunk_counts_by_area, tokens_input, tokens_output):
                log_area_usage(user_id, session_id, aid, area_input, area_output)
        else:
            # Nenhuma área envolvida (contexto vazio, nenhuma área pedida) —
            # mesmo comportamento de sempre: um registro sem área associada.
            log_area_usage(user_id, session_id, None, tokens_input, tokens_output)

        result = {
            "response": response_text,
            "context_sources": context_sources,
            "area_ids": billing_area_ids,
            "message": message
        }
        if quota_warning:
            result["quota_warning"] = quota_warning
        return jsonify(result)

    except Exception as e:
        print(f"ERRO chat RAG: {e}")
        return jsonify({
            "response": f"Erro ao processar consulta: {str(e)}",
            "context_sources": [],
            "area_ids": requested_area_ids,
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
    """Lista todos os documentos com nome da área. Aceita ?area_id= e ?parent_doc_id=
    (parent_doc_id=0 filtra só documentos raiz, sem pai)."""
    area_id = request.args.get('area_id')
    parent_doc_id = request.args.get('parent_doc_id')
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível", "total": 0, "documents": []}), 500
    try:
        cur = conn.cursor()
        where = []
        params = []
        if area_id:
            where.append("d.area_id = %s")
            params.append(area_id)
        if parent_doc_id == '0':
            where.append("d.parent_doc_id IS NULL")
        elif parent_doc_id:
            where.append("d.parent_doc_id = %s")
            params.append(parent_doc_id)
        where_clause = ("WHERE " + " AND ".join(where)) if where else ""
        cur.execute(
            f"""SELECT d.id, d.name, a.name as area_name, d.is_external_link, d.url,
                       d.processing_status, d.chunk_count, d.status, d.fetch_mode,
                       d.parent_doc_id, p.name as parent_name,
                       (SELECT count(*) FROM documents c WHERE c.parent_doc_id = d.id) as child_count
                FROM documents d JOIN areas a ON a.id = d.area_id
                LEFT JOIN documents p ON p.id = d.parent_doc_id
                {where_clause}
                ORDER BY d.upload_date DESC""",
            params
        )
        rows = cur.fetchall()
        docs = []
        for r in rows:
            docs.append({
                "id": r[0], "name": r[1], "area_name": r[2],
                "type": "link" if r[3] else "file", "url": r[4] or "",
                "processing_status": r[5] or "pending",
                "chunk_count": r[6] or 0,
                "status": r[7] or "active",
                "fetch_mode": r[8] or "http",
                "parent_doc_id": r[9],
                "parent_name": r[10],
                "child_count": r[11] or 0
            })
        conn.close()
        return jsonify({"total": len(docs), "documents": docs})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/documents/<int:doc_id>', methods=['GET'])
def admin_get_document(doc_id):
    """Detalhe completo de um documento, incluindo o texto extraído — usado
    pelo modal de edição no painel admin."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT d.id, d.name, d.url, d.area_id, a.name, d.status, d.fetch_mode,
                      d.is_external_link, d.processing_status, d.chunk_count, d.content_text
               FROM documents d JOIN areas a ON a.id = d.area_id
               WHERE d.id = %s""",
            (doc_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Documento não encontrado"}), 404
        return jsonify({
            "id": row[0], "name": row[1], "url": row[2] or "",
            "area_id": row[3], "area_name": row[4],
            "status": row[5] or "active", "fetch_mode": row[6] or "http",
            "type": "link" if row[7] else "file",
            "processing_status": row[8] or "pending", "chunk_count": row[9] or 0,
            "content_text": row[10] or ""
        })
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


@app.route('/admin/documents/<int:doc_id>', methods=['PATCH'])
def admin_update_document(doc_id):
    """Edita qualquer campo de um documento — nome, URL, área, status, método
    de busca (http/js_browser) e o texto já extraído.

    Trocar a URL ou o método de busca limpa o content_text existente (a menos
    que um novo content_text já venha junto no mesmo request), forçando uma
    nova extração da URL da próxima vez que 'Reprocessar RAG' for chamado —
    sem isso, o pipeline reaproveitaria o texto extraído com o método antigo."""
    data = request.get_json()
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT url, fetch_mode FROM documents WHERE id = %s", (doc_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Documento não encontrado"}), 404
        current_url, current_fetch_mode = row[0], row[1] or "http"

        fields = {}
        if 'name' in data:
            name = (data.get('name') or '').strip()
            if not name:
                conn.close()
                return jsonify({"error": "Nome não pode ser vazio"}), 400
            fields['name'] = name
        if 'url' in data:
            fields['url'] = (data.get('url') or '').strip() or None
        if 'status' in data and data.get('status'):
            fields['status'] = data.get('status')
        if 'fetch_mode' in data and data.get('fetch_mode'):
            fields['fetch_mode'] = data.get('fetch_mode')
        if 'area_id' in data and data.get('area_id'):
            new_area_id = data.get('area_id')
            cur.execute("SELECT id FROM areas WHERE id = %s AND status = 'active'", (new_area_id,))
            if not cur.fetchone():
                conn.close()
                return jsonify({"error": "Área não encontrada ou inativa"}), 404
            fields['area_id'] = new_area_id

        if 'content_text' in data:
            fields['content_text'] = data.get('content_text') or None
        else:
            url_changed = 'url' in fields and fields['url'] != current_url
            mode_changed = 'fetch_mode' in fields and fields['fetch_mode'] != current_fetch_mode
            if url_changed or mode_changed:
                fields['content_text'] = None
                fields['processing_status'] = 'pending'

        if not fields:
            conn.close()
            return jsonify({"error": "Nada para atualizar"}), 400

        set_clause = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(f"UPDATE documents SET {set_clause} WHERE id = %s", list(fields.values()) + [doc_id])

        if 'area_id' in fields:
            cur.execute("UPDATE document_chunks SET area_id = %s WHERE doc_id = %s", (fields['area_id'], doc_id))

        conn.commit()
        conn.close()
        return jsonify({"message": f"Documento {doc_id} atualizado"})
    except Exception as e:
        if conn: conn.rollback(); conn.close()
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


# ---- Clientes (identidade mínima para billing por token) ----

@app.route('/admin/users', methods=['GET'])
def admin_list_users():
    """Lista clientes cadastrados, com a chave de acesso completa (painel admin
    interno, mesmo nível de confiança que o resto do sistema — sem auth)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível", "total": 0, "users": []}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT u.id, u.email, u.api_key, u.created_at, u.plan_id, p.name
               FROM users u LEFT JOIN plans p ON p.id = u.plan_id
               ORDER BY u.email"""
        )
        rows = cur.fetchall()
        users = []
        for r in rows:
            users.append({
                "id": r[0], "email": r[1],
                "api_key": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
                "plan_id": r[4], "plan_name": r[5]
            })
        conn.close()
        return jsonify({"total": len(users), "users": users})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/users', methods=['POST'])
def admin_create_user():
    """Cria um cliente e gera sua chave de acesso."""
    data = request.get_json()
    email = (data.get('email') or '').strip()
    if not email:
        return jsonify({"error": "Email é obrigatório"}), 400
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        api_key = secrets.token_hex(32)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password_hash, api_key) VALUES (%s, %s, %s) RETURNING id",
            (email, "", api_key)
        )
        user_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": user_id, "email": email, "api_key": api_key}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"Email '{email}' já cadastrado"}), 409
    except Exception as e:
        if conn: conn.rollback(); conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/users/<int:user_id>', methods=['PATCH'])
def admin_update_user(user_id):
    """Atualiza o email, o plano e/ou regenera a chave de acesso de um cliente
    (regenerar invalida a chave antiga na hora — qualquer integração usando
    a chave anterior para de funcionar)."""
    data = request.get_json()
    email = (data.get('email') or '').strip() or None
    regenerate_key = bool(data.get('regenerate_key'))
    plan_id_given = 'plan_id' in data
    plan_id = data.get('plan_id') if plan_id_given else None
    if not email and not regenerate_key and not plan_id_given:
        return jsonify({"error": "Nada para atualizar"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()

        fields = {}
        if email:
            fields['email'] = email
        if regenerate_key:
            fields['api_key'] = secrets.token_hex(32)
        if plan_id_given:
            fields['plan_id'] = plan_id

        set_clause = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(f"UPDATE users SET {set_clause} WHERE id = %s", list(fields.values()) + [user_id])

        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Cliente não encontrado"}), 404

        conn.commit()
        cur.execute(
            """SELECT u.id, u.email, u.api_key, u.plan_id, p.name
               FROM users u LEFT JOIN plans p ON p.id = u.plan_id WHERE u.id = %s""",
            (user_id,)
        )
        row = cur.fetchone()
        conn.close()
        return jsonify({"id": row[0], "email": row[1], "api_key": row[2], "plan_id": row[3], "plan_name": row[4]})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"Email '{email}' já cadastrado"}), 409
    except Exception as e:
        if conn: conn.rollback(); conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/plans', methods=['GET'])
def admin_list_plans():
    """Lista planos com a tabela de preço por área e quantos clientes usam cada um."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, description FROM plans ORDER BY name")
        plans = [{"id": r[0], "name": r[1], "description": r[2]} for r in cur.fetchall()]

        for plan in plans:
            cur.execute(
                """SELECT pap.area_id, a.name, pap.monthly_token_quota, pap.price_per_1k_tokens
                   FROM plan_area_pricing pap JOIN areas a ON a.id = pap.area_id
                   WHERE pap.plan_id = %s ORDER BY a.name""",
                (plan["id"],)
            )
            plan["areas"] = [{
                "area_id": r[0], "area_name": r[1],
                "monthly_token_quota": r[2],
                "price_per_1k_tokens": float(r[3]) if r[3] is not None else None
            } for r in cur.fetchall()]
            cur.execute("SELECT count(*) FROM users WHERE plan_id = %s", (plan["id"],))
            plan["user_count"] = cur.fetchone()[0]

        conn.close()
        return jsonify({"total": len(plans), "plans": plans})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


def _replace_plan_area_pricing(cur, plan_id, areas):
    """Apaga e regrava as linhas de plan_area_pricing de um plano — o formulário
    sempre manda a tabela completa, então substituir tudo é mais simples e
    seguro do que tentar diffar upsert/delete linha a linha."""
    cur.execute("DELETE FROM plan_area_pricing WHERE plan_id = %s", (plan_id,))
    for a in (areas or []):
        area_id = a.get('area_id')
        if not area_id:
            continue
        cur.execute(
            """INSERT INTO plan_area_pricing (plan_id, area_id, monthly_token_quota, price_per_1k_tokens)
               VALUES (%s, %s, %s, %s)""",
            (plan_id, area_id, a.get('monthly_token_quota'), a.get('price_per_1k_tokens'))
        )


@app.route('/admin/plans', methods=['POST'])
def admin_create_plan():
    """Cria um plano com nome/descrição e a tabela de preço por área."""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "Nome é obrigatório"}), 400
    description = data.get('description')

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO plans (name, description) VALUES (%s, %s) RETURNING id",
            (name, description)
        )
        plan_id = cur.fetchone()[0]
        _replace_plan_area_pricing(cur, plan_id, data.get('areas'))
        conn.commit()
        conn.close()
        return jsonify({"id": plan_id, "name": name}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"Já existe um plano chamado '{name}'"}), 409
    except Exception as e:
        if conn: conn.rollback(); conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/plans/<int:plan_id>', methods=['PATCH'])
def admin_update_plan(plan_id):
    """Atualiza nome/descrição de um plano e substitui sua tabela de preço por área.
    Vínculo ao vivo: clientes nesse plano já refletem os novos valores na próxima
    checagem de cota, sem precisar reatribuir nada."""
    data = request.get_json()
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        fields = {}
        if 'name' in data:
            name = (data.get('name') or '').strip()
            if not name:
                conn.close()
                return jsonify({"error": "Nome não pode ser vazio"}), 400
            fields['name'] = name
        if 'description' in data:
            fields['description'] = data.get('description')

        if fields:
            set_clause = ", ".join(f"{k} = %s" for k in fields)
            cur.execute(f"UPDATE plans SET {set_clause} WHERE id = %s", list(fields.values()) + [plan_id])
            if cur.rowcount == 0:
                conn.close()
                return jsonify({"error": "Plano não encontrado"}), 404

        if 'areas' in data:
            _replace_plan_area_pricing(cur, plan_id, data.get('areas'))

        conn.commit()
        conn.close()
        return jsonify({"message": f"Plano {plan_id} atualizado"})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({"error": "Já existe um plano com esse nome"}), 409
    except Exception as e:
        if conn: conn.rollback(); conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/plans/<int:plan_id>', methods=['DELETE'])
def admin_delete_plan(plan_id):
    """Exclui um plano. Clientes nesse plano voltam para 'sem plano'
    (ON DELETE SET NULL) — sem checagem de cota até serem reatribuídos."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM plans WHERE id = %s", (plan_id,))
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Plano não encontrado"}), 404
        conn.close()
        return jsonify({"message": f"Plano {plan_id} excluído"})
    except Exception as e:
        if conn: conn.rollback(); conn.close()
        return jsonify({"error": str(e)}), 500


def _period_bounds(period):
    """Retorna (início, fim) de um período 'YYYY-MM'; usa o mês corrente se period for None."""
    if period:
        y, m = period.split("-")
        start = date(int(y), int(m), 1)
    else:
        today = date.today()
        start = date(today.year, today.month, 1)
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start, end


@app.route('/admin/usage-summary', methods=['GET'])
def admin_usage_summary():
    """Resumo de consumo de tokens do período — total, por área, por cliente, com custo estimado."""
    period = request.args.get('period')
    try:
        start, end = _period_bounds(period)
    except Exception:
        return jsonify({"error": "period inválido, use o formato YYYY-MM"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()

        cur.execute(
            """SELECT COALESCE(SUM(tokens_input),0), COALESCE(SUM(tokens_output),0), COUNT(*)
               FROM usage_logs WHERE timestamp >= %s AND timestamp < %s""",
            (start, end)
        )
        total_in, total_out, total_req = cur.fetchone()

        cur.execute(
            """SELECT a.id, a.name, COALESCE(SUM(u.tokens_input),0), COALESCE(SUM(u.tokens_output),0), COUNT(u.id)
               FROM usage_logs u JOIN areas a ON a.id = u.area_id
               WHERE u.timestamp >= %s AND u.timestamp < %s
               GROUP BY a.id, a.name ORDER BY a.name""",
            (start, end)
        )
        by_area = [{"area_id": r[0], "name": r[1], "tokens_input": r[2], "tokens_output": r[3], "requests": r[4]} for r in cur.fetchall()]

        cur.execute(
            """SELECT u.user_id, us.email, u.area_id, a.name, COALESCE(SUM(u.tokens_input),0), COALESCE(SUM(u.tokens_output),0),
                      COUNT(u.id), MAX(s.price_per_1k_tokens), MAX(s.monthly_token_quota)
               FROM usage_logs u
               JOIN users us ON us.id = u.user_id
               JOIN areas a ON a.id = u.area_id
               LEFT JOIN plan_area_pricing s ON s.plan_id = us.plan_id AND s.area_id = u.area_id
               WHERE u.timestamp >= %s AND u.timestamp < %s AND u.user_id IS NOT NULL
               GROUP BY u.user_id, us.email, u.area_id, a.name ORDER BY us.email""",
            (start, end)
        )
        by_user = []
        total_cost = 0.0
        for uid, email, aid, area_name, tin, tout, reqs, price, quota in cur.fetchall():
            total_tokens = tin + tout
            cost = round(total_tokens / 1000 * float(price), 2) if price is not None else None
            if cost is not None:
                total_cost += cost
            pct_used = round(total_tokens / quota * 100, 1) if quota else None
            by_user.append({
                "user_id": uid, "email": email, "area_id": aid, "area_name": area_name,
                "tokens_input": tin, "tokens_output": tout, "requests": reqs,
                "estimated_cost": cost,
                "monthly_token_quota": quota,
                "quota_pct_used": pct_used
            })

        conn.close()
        return jsonify({
            "period": start.strftime("%Y-%m"),
            "total_tokens_input": total_in,
            "total_tokens_output": total_out,
            "total_requests": total_req,
            "total_estimated_cost": round(total_cost, 2),
            "by_area": by_area,
            "by_user": by_user
        })
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/usage-report', methods=['GET'])
def admin_usage_report():
    """Série de consumo agrupada por dia, cliente ou área, com filtros opcionais."""
    user_id = request.args.get('user_id', type=int)
    area_id = request.args.get('area_id', type=int)
    date_from = request.args.get('from')
    date_to = request.args.get('to')
    group_by = request.args.get('group_by', 'day')

    if group_by not in ('day', 'user', 'area'):
        return jsonify({"error": "group_by deve ser day, user ou area"}), 400

    conditions = []
    params = []
    if user_id:
        conditions.append("u.user_id = %s")
        params.append(user_id)
    if area_id:
        conditions.append("u.area_id = %s")
        params.append(area_id)
    if date_from:
        conditions.append("u.timestamp >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("u.timestamp < %s")
        params.append(date_to)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    bucket_expr = {"day": "u.timestamp::date", "user": "us.email", "area": "a.name"}[group_by]

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        sql = f"""
            SELECT {bucket_expr},
                   COALESCE(SUM(u.tokens_input),0), COALESCE(SUM(u.tokens_output),0), COUNT(u.id)
            FROM usage_logs u
            LEFT JOIN users us ON us.id = u.user_id
            LEFT JOIN areas a ON a.id = u.area_id
            {where}
            GROUP BY {bucket_expr}
            ORDER BY {bucket_expr}
        """
        cur.execute(sql, params)
        rows = [{"bucket": str(r[0]), "tokens_input": r[1], "tokens_output": r[2], "requests": r[3]} for r in cur.fetchall()]
        conn.close()
        return jsonify({"rows": rows})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/quota-status', methods=['GET'])
def admin_quota_status_route():
    """Status de cota (usado/restante/preço) de um cliente numa área."""
    user_id = request.args.get('user_id', type=int)
    area_id = request.args.get('area_id', type=int)
    if not user_id or not area_id:
        return jsonify({"error": "user_id e area_id são obrigatórios"}), 400
    status = get_quota_status(user_id, area_id)
    if status is None:
        return jsonify({"error": "Nenhuma assinatura configurada para esse cliente+área"}), 404
    return jsonify(status)


@app.route('/admin/config', methods=['GET'])
def admin_get_config():
    """Configuração atual para exibição — nunca devolve segredos de verdade,
    só se estão configurados ou não (edição é feita via POST /admin/config)."""
    llm = CONFIG["llm"]
    db = CONFIG["database"]
    return jsonify({
        "llm": {
            "provider": llm.get("provider"),
            "base_url": llm.get("base_url"),
            "model": llm.get("model"),
            "api_key_configured": bool(llm.get("api_key")),
            "temperature": llm.get("temperature"),
            "max_tokens": llm.get("max_tokens"),
            "timeout_seconds": llm.get("timeout_seconds"),
        },
        "database": {
            "host": db.get("host"),
            "dbname": db.get("dbname"),
            "user": db.get("user"),
            "password_configured": bool(db.get("password")),
        },
        "tutor": {
            "quota_enforcement": QUOTA_ENFORCEMENT,
        },
        "monitor_agent": CONFIG.get("monitor_agent", {}),
        "backup_manager": CONFIG.get("backup_manager", {}),
    })


@app.route('/admin/config', methods=['POST'])
def admin_save_config():
    """Grava alterações em config.yaml. Segredos (api_key/password) só são
    sobrescritos quando vêm preenchidos no payload — campo ausente ou vazio
    mantém o valor atual. Não aplica sozinho: os serviços rodando neste
    processo já carregaram o config.yaml antigo, é preciso reiniciar
    (POST /admin/config/restart) pra valer."""
    payload = request.get_json(silent=True) or {}
    new_config = copy.deepcopy(CONFIG)

    llm_in = payload.get("llm") or {}
    llm = new_config.setdefault("llm", {})
    for field in ("provider", "base_url", "model"):
        if field in llm_in:
            llm[field] = llm_in[field] or None
    if "temperature" in llm_in and llm_in["temperature"] not in (None, ""):
        llm["temperature"] = float(llm_in["temperature"])
    for field in ("max_tokens", "timeout_seconds"):
        if field in llm_in and llm_in[field] not in (None, ""):
            llm[field] = int(llm_in[field])
    if "api_key" in llm_in:
        llm["api_key"] = llm_in["api_key"] or None

    db_in = payload.get("database") or {}
    db = new_config.setdefault("database", {})
    for field in ("host", "dbname", "user"):
        if field in db_in:
            db[field] = db_in[field] or None
    if "port" in db_in and db_in["port"] not in (None, ""):
        db["port"] = int(db_in["port"])
    if "password" in db_in:
        db["password"] = db_in["password"] or None

    tutor_in = payload.get("tutor") or {}
    if "quota_enforcement" in tutor_in:
        new_config.setdefault("tutor", {})["quota_enforcement"] = tutor_in["quota_enforcement"]

    monitor_in = payload.get("monitor_agent") or {}
    if monitor_in:
        monitor = new_config.setdefault("monitor_agent", {})
        if "fetch_timeout" in monitor_in and monitor_in["fetch_timeout"] not in (None, ""):
            monitor["fetch_timeout"] = int(monitor_in["fetch_timeout"])
        if "default_cron" in monitor_in:
            monitor["default_cron"] = monitor_in["default_cron"]

    backup_in = payload.get("backup_manager") or {}
    if "backup_dir" in backup_in:
        new_config.setdefault("backup_manager", {})["backup_dir"] = backup_in["backup_dir"]

    try:
        save_config(new_config)
    except Exception as e:
        return jsonify({"error": f"Falha ao gravar config.yaml: {e}"}), 500

    return jsonify({"success": True, "message": "Configuração salva em config.yaml. Reinicie os serviços para aplicar."})


@app.route('/admin/config/restart', methods=['POST'])
def admin_restart_services():
    """Reinicia os 3 serviços pra aplicar o config.yaml salvo. Roda em segundo
    plano com um pequeno atraso porque este processo (ai-tutor-api) está entre
    os que serão reiniciados — sem o atraso, a resposta HTTP não chegaria a
    sair antes do processo morrer."""
    try:
        subprocess.Popen(
            ["bash", "-c", "sleep 1 && systemctl restart ai-tutor-api monitor-agent backup-manager"],
            start_new_session=True
        )
    except Exception as e:
        return jsonify({"error": f"Falha ao reiniciar serviços: {e}"}), 500
    return jsonify({"success": True, "message": "Reiniciando serviços..."})


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
