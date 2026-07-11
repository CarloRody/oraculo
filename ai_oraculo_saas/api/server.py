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
    """Retorna só as áreas que o plano do cliente atual realmente inclui
    (ver get_plan_area_ids) — sem chave válida ou sem plano atribuído,
    nenhuma área é retornada."""
    user_id = resolve_user_from_request()
    area_ids = get_plan_area_ids(user_id)
    if not area_ids:
        return jsonify({"areas": []})

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Não foi possível conectar ao banco de dados"}), 500

    try:
        cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(area_ids))
        cur.execute(f"SELECT id, name, slug FROM areas WHERE id IN ({placeholders}) ORDER BY name", area_ids)
        rows = cur.fetchall()
        areas = [{"id": r[0], "name": r[1], "slug": r[2]} for r in rows]

        conn.close()
        return jsonify({"areas": areas})

    except Exception as e:
        if conn:
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/api/my-area', methods=['GET'])
def get_my_area():
    """Base de conhecimento privada de quem está chamando, identificado pela
    X-Oraculo-Key — usado por meu-portal.html pra saber onde o cliente pode
    subir conteúdo. area=null quando o admin ainda não criou uma pra ele."""
    api_key = request.headers.get('X-Oraculo-Key')
    if not api_key:
        return jsonify({"error": "Chave de acesso é obrigatória"}), 401

    user_id = resolve_user_from_request()
    if not user_id:
        return jsonify({"error": "Chave de acesso inválida"}), 401

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, slug FROM areas WHERE owner_user_id = %s AND status = 'active'",
            (user_id,)
        )
        row = cur.fetchone()
        conn.close()
        area = {"id": row[0], "name": row[1], "slug": row[2]} if row else None
        return jsonify({"area": area})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/api/me', methods=['GET'])
def get_me():
    """Identifica o cliente pela X-Oraculo-Key — usado pra mostrar o nome de
    quem está conectado (barra de login/logout do index.html e outras
    páginas voltadas pro cliente)."""
    api_key = request.headers.get('X-Oraculo-Key')
    if not api_key:
        return jsonify({"error": "Chave de acesso é obrigatória"}), 401

    user_id = resolve_user_from_request()
    if not user_id:
        return jsonify({"error": "Chave de acesso inválida"}), 401

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT email FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Cliente não encontrado"}), 404
        return jsonify({"email": row[0]})
    except Exception as e:
        if conn: conn.close()
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

    auth_error = authorize_client_area_write(area_id)
    if auth_error:
        return auth_error

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


def get_plan_area_ids(user_id):
    """Áreas que o plano atual do cliente realmente inclui (têm linha em
    plan_area_pricing) — isso define o que o cliente pode ver/perguntar, não
    mais 'toda área global'. Sem chave, sem plano ou plano sem nenhuma área
    precificada = lista vazia (nenhuma área)."""
    if not user_id:
        return []
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT pap.area_id FROM users u
               JOIN plan_area_pricing pap ON pap.plan_id = u.plan_id
               JOIN areas a ON a.id = pap.area_id
               WHERE u.id = %s AND a.status = 'active'""",
            (user_id,)
        )
        area_ids = [r[0] for r in cur.fetchall()]
        conn.close()
        return area_ids
    except Exception as e:
        if conn: conn.close()
        print(f"ERRO get_plan_area_ids: {e}")
        return []


def resolve_authorized_area_ids(requested_area_ids, user_id):
    """Restringe requested_area_ids às áreas que o plano dessa chave inclui.
    requested_area_ids=None ("todas as áreas") vira explicitamente a lista de
    áreas do plano — nunca deixa "todas" significar mais do que o plano cobre.

    Retorna (area_ids_autorizados, houve_pedido_negado). houve_pedido_negado
    só é True quando area_ids específicos foram pedidos e pelo menos um foi
    recusado — usado pelo chamador pra escolher a mensagem de erro certa."""
    plan_area_ids = set(get_plan_area_ids(user_id))
    if requested_area_ids:
        authorized = [aid for aid in requested_area_ids if aid in plan_area_ids]
        had_unauthorized = len(authorized) < len(requested_area_ids)
        return authorized, had_unauthorized
    return list(plan_area_ids), False


def authorize_client_area_write(area_id):
    """Trava de escrita pro upload self-service do cliente (meu-portal.html).

    Se a requisição vier SEM X-Oraculo-Key, é uso interno (extract.html/admin
    na rede local) — não restringe nada, comportamento de sempre. Se vier COM
    a chave, é contexto de cliente: só libera escrever se `area_id` for
    exatamente a base de conhecimento privada daquele cliente (chave inválida
    também é rejeitada — nunca tratamos "chave que não bateu" como "sem
    chave", senão uma chave errada acabaria com mais acesso que uma certa).

    Retorna None se autorizado, ou uma tupla (response, status) pronta pra
    devolver direto se não for.
    """
    api_key = request.headers.get('X-Oraculo-Key')
    if not api_key:
        return None

    user_id = resolve_user_from_request()
    if not user_id:
        return jsonify({"error": "Chave de acesso inválida"}), 401

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM areas WHERE owner_user_id = %s", (user_id,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500

    try:
        area_id_int = int(area_id) if area_id else None
    except (TypeError, ValueError):
        area_id_int = None

    if not row or area_id_int != row[0]:
        return jsonify({"error": "Você só pode adicionar conteúdo na sua própria base de conhecimento"}), 403

    return None


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


def resolve_llm_config_for_user(user_id):
    """Resolve base_url/api_key/model/temperatura/max_tokens/timeout e o preço
    do modelo de IA do plano do cliente (roteamento real — cada plano pode
    chamar uma API diferente). Sem plano, plano sem modelo, ou sem user_id
    (uso interno via extract.html/rag.html) cai no LLM_CONFIG global de
    config.yaml, sem preço nenhum (sem cobrança de crédito)."""
    if user_id:
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    """SELECT m.id, m.base_url, m.api_key, m.model_name, m.temperature,
                              m.max_tokens, m.timeout_seconds, m.price_input_per_million,
                              m.price_output_per_million, m.markup_percentage
                       FROM users u JOIN plans p ON p.id = u.plan_id
                       JOIN ai_models m ON m.id = p.model_id
                       WHERE u.id = %s AND m.status = 'active'""",
                    (user_id,)
                )
                row = cur.fetchone()
                conn.close()
                if row:
                    return {
                        "model_row_id": row[0], "base_url": row[1], "api_key": row[2],
                        "model": row[3],
                        "temperature": float(row[4]) if row[4] is not None else LLM_CONFIG.get("temperature", 0.7),
                        "max_tokens": row[5] or LLM_CONFIG.get("max_tokens", 6000),
                        "timeout_seconds": row[6] or LLM_CONFIG.get("timeout_seconds", 600),
                        "price_input_per_million": float(row[7]),
                        "price_output_per_million": float(row[8]),
                        "markup_percentage": float(row[9]),
                    }
            except Exception as e:
                if conn: conn.close()
                print(f"ERRO resolve_llm_config_for_user: {e}")
    return {
        "model_row_id": None, "base_url": LLM_CONFIG["base_url"], "api_key": LLM_CONFIG.get("api_key"),
        "model": LLM_CONFIG.get("model", "auto"),
        "temperature": LLM_CONFIG.get("temperature", 0.7),
        "max_tokens": LLM_CONFIG.get("max_tokens", 6000),
        "timeout_seconds": LLM_CONFIG.get("timeout_seconds", 600),
        "price_input_per_million": None, "price_output_per_million": None, "markup_percentage": None,
    }


def compute_consumption_value(llm_cfg, tokens_input, tokens_output):
    """Valor em R$ a debitar do saldo do cliente por esta resposta, ou None se
    o modelo resolvido não tem preço (uso interno/sem plano — não cobra)."""
    if llm_cfg.get("price_input_per_million") is None:
        return None
    base = (tokens_input / 1_000_000 * llm_cfg["price_input_per_million"]
            + tokens_output / 1_000_000 * llm_cfg["price_output_per_million"])
    return round(base * (1 + llm_cfg["markup_percentage"] / 100), 4)


def apply_credit_transaction(user_id, amount, type_, description, session_id=None, tokens_input=None, tokens_output=None):
    """Grava uma linha no ledger de créditos e atualiza users.balance
    atomicamente — SELECT ... FOR UPDATE trava a linha do cliente durante a
    transação, evitando corrida entre duas respostas de chat concorrentes do
    mesmo cliente debitarem em cima uma da outra. Retorna o saldo novo, ou
    None em erro (nunca levanta)."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT balance FROM users WHERE id = %s FOR UPDATE", (user_id,))
        row = cur.fetchone()
        if not row:
            conn.rollback()
            conn.close()
            return None
        new_balance = float(row[0]) + amount
        cur.execute("UPDATE users SET balance = %s WHERE id = %s", (new_balance, user_id))
        cur.execute(
            """INSERT INTO credit_transactions
               (user_id, type, amount, balance_after, description, session_id, tokens_input, tokens_output)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (user_id, type_, amount, new_balance, description, session_id, tokens_input, tokens_output)
        )
        conn.commit()
        conn.close()
        return new_balance
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        print(f"ERRO apply_credit_transaction: {e}")
        return None


def get_user_balance(user_id):
    """Saldo atual do cliente, ou None se não encontrado/erro."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        conn.close()
        return float(row[0]) if row else None
    except Exception as e:
        if conn: conn.close()
        print(f"ERRO get_user_balance: {e}")
        return None


def get_recent_consumption(user_id, limit=5):
    """Últimos consumos do cliente, pro extrato mostrado quando o saldo zera."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT created_at, amount, tokens_input, tokens_output, description
               FROM credit_transactions WHERE user_id = %s AND type = 'consumption'
               ORDER BY created_at DESC LIMIT %s""",
            (user_id, limit)
        )
        rows = cur.fetchall()
        conn.close()
        return [{"date": r[0].isoformat(), "amount": float(r[1]), "tokens_input": r[2],
                  "tokens_output": r[3], "description": r[4]} for r in rows]
    except Exception as e:
        if conn: conn.close()
        print(f"ERRO get_recent_consumption: {e}")
        return []


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


def build_rag_context(context_chunks, requested_area_ids):
    """A partir dos chunks retornados por search_similar, monta o texto de
    contexto (agrupado por área, com cabeçalho por seção — evita o modelo
    misturar regras de domínios diferentes) e a lista de fontes pra exibição.
    Usado tanto por /api/chat quanto por /api/agent-research. Devolve
    (full_context_text, context_sources, chunk_counts_by_area, billing_area_ids)."""
    chunk_counts_by_area = {}
    for chunk in context_chunks:
        aid = chunk["area_id"]
        chunk_counts_by_area[aid] = chunk_counts_by_area.get(aid, 0) + 1
    billing_area_ids = list(chunk_counts_by_area.keys()) or (requested_area_ids or [])

    conn = get_db_connection()
    doc_names = {}
    area_names = {}
    if conn and context_chunks:
        cur = conn.cursor()
        doc_ids = list(set(r["doc_id"] for r in context_chunks))
        if doc_ids:
            placeholders = ",".join(["%s"] * len(doc_ids))
            cur.execute(f"SELECT id, name FROM documents WHERE id IN ({placeholders})", doc_ids)
            doc_names = {row[0]: row[1] for row in cur.fetchall()}
        for aid in chunk_counts_by_area:
            area_names[aid] = _area_name(aid)
    if conn:
        conn.close()

    context_sources = []
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

    full_context_text = "\n\n".join(context_sections)
    return full_context_text, context_sources, chunk_counts_by_area, billing_area_ids


def call_llm_agent(llm_cfg, system_prompt, user_prompt):
    """Chama o LLM (base_url/model/parâmetros do llm_cfg resolvido pra esse
    cliente/plano) e devolve (texto, tokens_input, tokens_output). Usa o
    'usage' do gateway (exato) quando disponível, senão estima via tiktoken
    (count_tokens). Compartilhado por /api/chat e /api/agent-research."""
    llm_headers = {"Authorization": f"Bearer {llm_cfg['api_key']}"} if llm_cfg.get("api_key") else {}
    llm_response = _http_requests.post(
        llm_cfg["base_url"],
        json={
            "model": llm_cfg["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": llm_cfg["temperature"],
            "max_tokens": llm_cfg["max_tokens"]
        },
        headers=llm_headers,
        timeout=llm_cfg["timeout_seconds"]
    )
    llm_response.raise_for_status()
    llm_data = llm_response.json()
    text = llm_data["choices"][0]["message"]["content"]

    usage = llm_data.get("usage") or {}
    tokens_input = usage.get("prompt_tokens")
    tokens_output = usage.get("completion_tokens")
    if tokens_input is None or tokens_output is None:
        tokens_input = count_tokens(system_prompt + user_prompt)
        tokens_output = count_tokens(text)
    return text, tokens_input, tokens_output


def web_search(query, max_results=8):
    """Consulta o SearXNG local (container Docker, só acessível em
    localhost — evita expor publicamente e evita CORS) e devolve resultados
    (title, url, content). Nunca levanta — lista vazia em qualquer falha
    (SearXNG fora do ar, timeout, etc.). Corte de 6000 caracteres por
    resultado — na prática o "content" do SearXNG é um snippet do motor de
    busca (geralmente bem mais curto que isso), então esse limite quase
    nunca é atingido; ele só evita um resultado atípico de estourar o
    prompt sem necessidade."""
    try:
        resp = _http_requests.get(
            "http://127.0.0.1:8888/search",
            params={"q": query, "format": "json"},
            timeout=15
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])[:max_results]
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                  "content": (r.get("content") or "")[:6000]} for r in results]
    except Exception as e:
        print(f"ERRO web_search: {e}")
        return []


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

    # Restringe às áreas que essa chave pode ver — sem isso, uma área privada
    # de outro cliente poderia vazar tanto por ID explícito quanto por
    # omissão (area_ids vazio = "todas as áreas", que sem esse filtro
    # incluiria literalmente todas, inclusive as privadas de terceiros).
    authorized_area_ids, had_unauthorized_request = resolve_authorized_area_ids(requested_area_ids, user_id)
    if not authorized_area_ids:
        msg = ("Nenhuma das áreas pedidas está disponível pra essa chave de acesso."
               if had_unauthorized_request else
               "Nenhuma área disponível no seu plano de acesso.")
        return jsonify({"error": msg}), 403

    # Modelo de IA do plano do cliente (roteamento real) + preço em R$/1M
    # tokens. Sem plano/modelo = cai no LLM_CONFIG global, sem cobrança.
    llm_cfg = resolve_llm_config_for_user(user_id)
    if llm_cfg["price_input_per_million"] is not None:
        current_balance = get_user_balance(user_id)
        if current_balance is not None and current_balance <= 0:
            return jsonify({
                "error": "Seus créditos acabaram.",
                "credit_status": {
                    "balance": round(current_balance, 4),
                    "depleted": True,
                    "recent": get_recent_consumption(user_id)
                }
            }), 402

    try:
        # Busca chunks relevantes via RAG (uma ou várias áreas autorizadas —
        # já garantido não-vazio pelo retorno 403 acima)
        context_chunks = search_similar(message, area_ids=authorized_area_ids, top_k=50)
        full_context_text, context_sources, chunk_counts_by_area, billing_area_ids = build_rag_context(context_chunks, requested_area_ids)

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

        # Chama LLM — base_url/model/parâmetros vêm do modelo do plano do
        # cliente (llm_cfg, roteamento real) ou do LLM_CONFIG global se o
        # cliente não tiver plano/modelo associado.
        response_text, tokens_input, tokens_output = call_llm_agent(llm_cfg, system_prompt, user_prompt)

        session_id = log_chat_message(user_id, billing_area_ids, message, response_text, tokens_input, tokens_output)
        if billing_area_ids:
            for aid, area_input, area_output in split_tokens_by_area(billing_area_ids, chunk_counts_by_area, tokens_input, tokens_output):
                log_area_usage(user_id, session_id, aid, area_input, area_output)
        else:
            # Nenhuma área envolvida (contexto vazio, nenhuma área pedida) —
            # mesmo comportamento de sempre: um registro sem área associada.
            log_area_usage(user_id, session_id, None, tokens_input, tokens_output)

        # Débito do saldo em créditos — só cobra se o plano do cliente tem um
        # modelo com preço configurado (llm_cfg vindo de ai_models via plano).
        credit_status = None
        consumption_value = compute_consumption_value(llm_cfg, tokens_input, tokens_output)
        if consumption_value is not None:
            new_balance = apply_credit_transaction(
                user_id, -consumption_value, 'consumption',
                f"Chat — {llm_cfg['model']}", session_id=session_id,
                tokens_input=tokens_input, tokens_output=tokens_output
            )
            if new_balance is not None:
                # 4 casas decimais, não 2 — com preço por 1M tokens, o custo de
                # uma única mensagem costuma ser fração de centavo; arredondar
                # pra 2 casas escondia a dedução real (saldo parecia "não mudar").
                credit_status = {"balance": round(new_balance, 4), "depleted": new_balance <= 0}
                if credit_status["depleted"]:
                    credit_status["recent"] = get_recent_consumption(user_id)

        result = {
            "response": response_text,
            "context_sources": context_sources,
            "area_ids": billing_area_ids,
            "message": message
        }
        if quota_warning:
            result["quota_warning"] = quota_warning
        if credit_status:
            result["credit_status"] = credit_status
        return jsonify(result)

    except Exception as e:
        print(f"ERRO chat RAG: {e}")
        return jsonify({
            "response": f"Erro ao processar consulta: {str(e)}",
            "context_sources": [],
            "area_ids": requested_area_ids,
            "error": str(e)
        })


@app.route('/api/agent-research', methods=['POST'])
def agent_research():
    """Pesquisa com 3 agentes: o primeiro responde só com a documentação
    oficial (RAG, mesma trava de áreas do /api/chat), o segundo pesquisa a
    pergunta na internet (SearXNG local) e responde com o que encontrar, e o
    terceiro compara as duas respostas e produz a resposta final — a
    documentação oficial é a fonte de verdade principal, sempre prevalece em
    caso de conflito com a internet."""
    data = request.get_json()
    message = data.get('message', '')
    requested_area_ids = resolve_area_ids(data)
    user_id = resolve_user_from_request()

    if not message:
        return jsonify({"error": "Campo 'message' é obrigatório"}), 400

    authorized_area_ids, had_unauthorized_request = resolve_authorized_area_ids(requested_area_ids, user_id)
    if not authorized_area_ids:
        msg = ("Nenhuma das áreas pedidas está disponível pra essa chave de acesso."
               if had_unauthorized_request else
               "Nenhuma área disponível no seu plano de acesso.")
        return jsonify({"error": msg}), 403

    llm_cfg = resolve_llm_config_for_user(user_id)
    if llm_cfg["price_input_per_million"] is not None:
        current_balance = get_user_balance(user_id)
        if current_balance is not None and current_balance <= 0:
            return jsonify({
                "error": "Seus créditos acabaram.",
                "credit_status": {
                    "balance": round(current_balance, 4),
                    "depleted": True,
                    "recent": get_recent_consumption(user_id)
                }
            }), 402

    try:
        # ---- Agente 1: Documentação Oficial (RAG) ----
        context_chunks = search_similar(message, area_ids=authorized_area_ids, top_k=50)
        full_context_text, official_sources, chunk_counts_by_area, billing_area_ids = build_rag_context(context_chunks, requested_area_ids)

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

        official_system_prompt = (
            "Você é um assistente que responde exclusivamente com base na documentação "
            "oficial fornecida abaixo. Se a documentação não cobrir algum aspecto da "
            "pergunta, diga isso explicitamente — não complete com conhecimento geral. "
            "Desenvolva a resposta com detalhes relevantes do contexto (explicações, "
            "exemplos, exceções, passos) sempre que a documentação tiver material pra "
            "isso — não seja desnecessariamente breve. Responda em português."
        )
        official_user_prompt = f"""Contexto da documentação oficial:
{'=' * 60}
{full_context_text if full_context_text else '(nenhum trecho relevante encontrado na documentação)'}
{'=' * 60}

Pergunta: {message}"""
        official_answer, tin1, tout1 = call_llm_agent(llm_cfg, official_system_prompt, official_user_prompt)

        # ---- Agente 2: Busca na Internet (SearXNG) ----
        # Inclui o nome da área (ex: nome do produto/módulo) na query de busca —
        # sem isso, uma pergunta que só faz sentido dentro do contexto da área
        # temática (ex: um termo específico de um produto) vira uma busca
        # genérica demais. Limitado a 2 áreas pra não poluir a query quando
        # "todas as áreas" contribuíram contexto. Só a busca ganha o nome da
        # área — o prompt mandado pro LLM (web_user_prompt) usa a pergunta
        # original, sem alteração.
        area_names_for_web = [_area_name(aid) for aid in billing_area_ids[:2]]
        web_query = f"{' '.join(area_names_for_web)} {message}".strip() if area_names_for_web else message
        web_results = web_search(web_query)
        web_context_lines = [f"[{i+1}] {r['title']} ({r['url']})\n{r['content']}" for i, r in enumerate(web_results)]
        web_context_text = "\n\n".join(web_context_lines) if web_context_lines else "Nenhum resultado de busca encontrado."
        web_system_prompt = (
            "Você é um assistente que responde com base em resultados de busca na "
            "internet fornecidos abaixo. Cite as fontes relevantes pelo número entre "
            "colchetes (ex: [1]). Se os resultados não forem suficientes pra responder, "
            "diga isso explicitamente. Combine as informações dos vários resultados numa "
            "resposta desenvolvida e completa — não seja desnecessariamente breve. "
            "Responda em português."
        )
        web_user_prompt = f"""Resultados de busca:
{'=' * 60}
{web_context_text}
{'=' * 60}

Pergunta: {message}"""
        web_answer, tin2, tout2 = call_llm_agent(llm_cfg, web_system_prompt, web_user_prompt)
        web_sources = [{"title": r["title"], "url": r["url"]} for r in web_results]

        # ---- Agente 3: Comparador ----
        cmp_system_prompt = (
            "Você é um agente comparador. Você recebe a mesma pergunta respondida por "
            "duas fontes: (1) documentação oficial da empresa — é a fonte de verdade "
            "principal, sempre prevalece em caso de conflito; (2) busca na internet — é "
            "uma fonte complementar, útil pra contextualizar ou preencher lacunas que a "
            "documentação não cobre. Compare as duas respostas, resolva divergências "
            "priorizando SEMPRE a documentação oficial, e produza uma resposta final "
            "única e completa — desenvolva os pontos relevantes das duas fontes em vez "
            "de resumir demais, mantendo tudo que for útil pra responder a pergunta com "
            "profundidade. Se houver alguma divergência relevante entre as duas fontes, "
            "aponte isso explicitamente na resposta. Responda em português."
        )
        cmp_user_prompt = f"""Pergunta original: {message}

Resposta da documentação oficial:
{official_answer}

Resposta da busca na internet:
{web_answer}

Produza a resposta final."""
        final_answer, tin3, tout3 = call_llm_agent(llm_cfg, cmp_system_prompt, cmp_user_prompt)

        # ---- Log de uso: total das 3 chamadas, mesmo padrão do /api/chat ----
        tokens_input_total = tin1 + tin2 + tin3
        tokens_output_total = tout1 + tout2 + tout3
        session_id = log_chat_message(user_id, billing_area_ids, message, final_answer, tokens_input_total, tokens_output_total)
        if billing_area_ids:
            for aid, area_input, area_output in split_tokens_by_area(billing_area_ids, chunk_counts_by_area, tokens_input_total, tokens_output_total):
                log_area_usage(user_id, session_id, aid, area_input, area_output)
        else:
            log_area_usage(user_id, session_id, None, tokens_input_total, tokens_output_total)

        # ---- Débito de crédito, consolidado numa única transação ----
        credit_status = None
        consumption_value = compute_consumption_value(llm_cfg, tokens_input_total, tokens_output_total)
        if consumption_value is not None:
            new_balance = apply_credit_transaction(
                user_id, -consumption_value, 'consumption',
                'Pesquisa 3 agentes', session_id=session_id,
                tokens_input=tokens_input_total, tokens_output=tokens_output_total
            )
            if new_balance is not None:
                credit_status = {"balance": round(new_balance, 4), "depleted": new_balance <= 0}
                if credit_status["depleted"]:
                    credit_status["recent"] = get_recent_consumption(user_id)

        result = {
            "final_answer": final_answer,
            "official_answer": official_answer,
            "official_sources": official_sources,
            "web_answer": web_answer,
            "web_sources": web_sources,
            "area_ids": billing_area_ids,
            "message": message
        }
        if quota_warning:
            result["quota_warning"] = quota_warning
        if credit_status:
            result["credit_status"] = credit_status
        return jsonify(result)

    except Exception as e:
        print(f"ERRO agent_research: {e}")
        return jsonify({"error": str(e)}), 500


# ---- Admin endpoints ----

@app.route('/admin/areas', methods=['GET'])
def admin_get_areas():
    """Lista TODAS as áreas (qualquer status) com contagem de documentos, pro
    admin poder gerenciar rascunho/arquivada — só as públicas (/api/areas)
    filtram por status='active'. owner_user_id/owner_email preenchidos = base
    de conhecimento privada de um cliente (não uma área global)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível", "total": 0, "areas": []}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT a.id, a.name, a.slug, a.status, a.owner_user_id, u.email
               FROM areas a LEFT JOIN users u ON u.id = a.owner_user_id
               ORDER BY a.name"""
        )
        rows = cur.fetchall()
        areas = []
        for r in rows:
            cur.execute("SELECT count(*) FROM documents WHERE area_id = %s", (r[0],))
            doc_count = cur.fetchone()[0]
            areas.append({
                "id": r[0], "name": r[1], "slug": r[2], "status": r[3], "doc_count": doc_count,
                "owner_user_id": r[4], "owner_email": r[5]
            })
        conn.close()
        return jsonify({"total": len(areas), "areas": areas})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/areas', methods=['POST'])
def admin_create_area():
    """Cria uma nova área temática. owner_user_id opcional (uso interno da ação
    "Criar base de conhecimento" da aba Clientes) — cria uma área privada,
    exclusiva daquele cliente, em vez de uma área global. status opcional
    (default 'active'); slug opcional (default derivado do nome)."""
    data = request.get_json()
    name = data.get('name', '').strip()
    owner_user_id = data.get('owner_user_id')
    status = data.get('status') or 'active'
    if status not in ('active', 'draft', 'archived'):
        return jsonify({"error": "Status inválido (use active, draft ou archived)"}), 400
    if not name:
        return jsonify({"error": "Nome é obrigatório"}), 400
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        slug = (data.get('slug') or '').strip() or name.lower().replace(' ', '-').replace('/', '-')
        vector_ref = f"area_{slug}_v1"
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO areas (name, slug, vector_ref, status, owner_user_id) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (name, slug, vector_ref, status, owner_user_id)
        )
        area_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": area_id, "name": name, "slug": slug, "status": status, "owner_user_id": owner_user_id}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"Slug '{slug}' já está em uso por outra área"}), 409
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/areas/<int:area_id>', methods=['PATCH'])
def admin_update_area(area_id):
    """Atualiza nome, slug, status e/ou proprietário de uma área. slug vazio =
    re-deriva do nome; owner_user_id ausente/null = área volta a ser global."""
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"error": "Nome é obrigatório"}), 400

    status = data.get('status') or 'active'
    if status not in ('active', 'draft', 'archived'):
        return jsonify({"error": "Status inválido (use active, draft ou archived)"}), 400

    owner_user_id = data.get('owner_user_id') or None

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        slug = (data.get('slug') or '').strip() or name.lower().replace(' ', '-').replace('/', '-')
        cur = conn.cursor()
        cur.execute(
            "UPDATE areas SET name = %s, slug = %s, status = %s, owner_user_id = %s WHERE id = %s",
            (name, slug, status, owner_user_id, area_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Área não encontrada"}), 404
        conn.close()
        return jsonify({"id": area_id, "name": name, "slug": slug, "status": status, "owner_user_id": owner_user_id})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"Slug '{slug}' já está em uso por outra área"}), 409
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/areas/<int:area_id>', methods=['DELETE'])
def admin_delete_area(area_id):
    """Arquiva uma área (soft delete). Usava status='inactive' antes, que
    violava a CHECK constraint da coluna (só aceita active/draft/archived) —
    todo clique em "Excluir" falhava silenciosamente com erro 500."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("UPDATE areas SET status = 'archived' WHERE id = %s", (area_id,))
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Área não encontrada"}), 404
        conn.close()
        return jsonify({"message": f"Área {area_id} arquivada"})
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
            """SELECT u.id, u.email, u.api_key, u.created_at, u.plan_id, p.name, u.balance
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
                "plan_id": r[4], "plan_name": r[5],
                "balance": float(r[6])
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
    """Atualiza o email, o plano e/ou a chave de acesso de um cliente —
    regenera aleatoriamente (regenerate_key) ou define um valor customizado
    (api_key). Trocar a chave invalida a antiga na hora — qualquer
    integração usando a chave anterior para de funcionar."""
    data = request.get_json()
    email = (data.get('email') or '').strip() or None
    regenerate_key = bool(data.get('regenerate_key'))
    custom_api_key = (data.get('api_key') or '').strip() or None
    plan_id_given = 'plan_id' in data
    plan_id = data.get('plan_id') if plan_id_given else None
    if not email and not regenerate_key and not custom_api_key and not plan_id_given:
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
        elif custom_api_key:
            fields['api_key'] = custom_api_key
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
        detail = f"Email '{email}' já cadastrado" if email else "Chave de acesso já está em uso por outro cliente"
        return jsonify({"error": detail}), 409
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
        cur.execute(
            """SELECT p.id, p.name, p.description, p.model_id, m.name
               FROM plans p LEFT JOIN ai_models m ON m.id = p.model_id ORDER BY p.name"""
        )
        plans = [{"id": r[0], "name": r[1], "description": r[2], "model_id": r[3], "model_name": r[4]} for r in cur.fetchall()]

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
    """Cria um plano com nome/descrição, modelo de IA e a tabela de preço por área."""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "Nome é obrigatório"}), 400
    description = data.get('description')
    model_id = data.get('model_id') or None

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO plans (name, description, model_id) VALUES (%s, %s, %s) RETURNING id",
            (name, description, model_id)
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
        if 'model_id' in data:
            fields['model_id'] = data.get('model_id') or None

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


# ---- Modelos de IA (cadastro usado pelos planos pra roteamento + preço) ----

def _model_row_to_dict(r):
    return {
        "id": r[0], "name": r[1], "base_url": r[2], "api_key": r[3], "model_name": r[4],
        "temperature": float(r[5]) if r[5] is not None else None,
        "max_tokens": r[6], "timeout_seconds": r[7],
        "price_input_per_million": float(r[8]), "price_output_per_million": float(r[9]),
        "markup_percentage": float(r[10]), "status": r[11]
    }


@app.route('/admin/models', methods=['GET'])
def admin_get_models():
    """Lista todos os modelos de IA cadastrados (qualquer status)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível", "total": 0, "models": []}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, name, base_url, api_key, model_name, temperature, max_tokens,
                      timeout_seconds, price_input_per_million, price_output_per_million,
                      markup_percentage, status
               FROM ai_models ORDER BY name"""
        )
        models = [_model_row_to_dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({"total": len(models), "models": models})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/models', methods=['POST'])
def admin_create_model():
    """Cria um modelo de IA (nome + endpoint + preço por 1M tokens + markup)."""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    base_url = (data.get('base_url') or '').strip()
    model_name = (data.get('model_name') or '').strip()
    if not name or not base_url or not model_name:
        return jsonify({"error": "Nome, base_url e model_name são obrigatórios"}), 400
    status = data.get('status') or 'active'
    if status not in ('active', 'inactive'):
        return jsonify({"error": "Status inválido (use active ou inactive)"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO ai_models
               (name, base_url, api_key, model_name, temperature, max_tokens, timeout_seconds,
                price_input_per_million, price_output_per_million, markup_percentage, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (name, base_url, data.get('api_key') or None, model_name,
             data.get('temperature'), data.get('max_tokens'), data.get('timeout_seconds'),
             data.get('price_input_per_million') or 0, data.get('price_output_per_million') or 0,
             data.get('markup_percentage') or 0, status)
        )
        model_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": model_id, "name": name}), 201
    except Exception as e:
        if conn: conn.rollback(); conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/models/<int:model_id>', methods=['PATCH'])
def admin_update_model(model_id):
    """Atualiza qualquer campo de um modelo de IA."""
    data = request.get_json()
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        allowed = (
            'name', 'base_url', 'api_key', 'model_name', 'temperature', 'max_tokens',
            'timeout_seconds', 'price_input_per_million', 'price_output_per_million',
            'markup_percentage', 'status'
        )
        fields = {}
        for k in allowed:
            if k in data:
                fields[k] = data.get(k)

        if 'name' in fields and not (fields['name'] or '').strip():
            conn.close()
            return jsonify({"error": "Nome não pode ser vazio"}), 400
        if 'status' in fields and fields['status'] not in ('active', 'inactive'):
            conn.close()
            return jsonify({"error": "Status inválido (use active ou inactive)"}), 400
        if not fields:
            conn.close()
            return jsonify({"error": "Nada para atualizar"}), 400

        set_clause = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(f"UPDATE ai_models SET {set_clause} WHERE id = %s", list(fields.values()) + [model_id])
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Modelo não encontrado"}), 404
        conn.close()
        return jsonify({"message": f"Modelo {model_id} atualizado"})
    except Exception as e:
        if conn: conn.rollback(); conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/admin/models/<int:model_id>', methods=['DELETE'])
def admin_delete_model(model_id):
    """Desativa um modelo (soft delete — planos que apontam pra ele passam a
    cair no LLM_CONFIG global até um novo modelo ser escolhido)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("UPDATE ai_models SET status = 'inactive' WHERE id = %s", (model_id,))
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Modelo não encontrado"}), 404
        conn.close()
        return jsonify({"message": f"Modelo {model_id} desativado"})
    except Exception as e:
        if conn: conn.rollback(); conn.close()
        return jsonify({"error": str(e)}), 500


# ---- Créditos (depósitos manuais do admin + extrato) ----

@app.route('/admin/credits/deposit', methods=['POST'])
def admin_deposit_credit():
    """Lança um depósito (ou ajuste manual, se amount for negativo) no saldo
    de um cliente. Não é usado pra registrar consumo — isso é feito só pelo
    /api/chat via apply_credit_transaction."""
    data = request.get_json()
    user_id = data.get('user_id')
    amount = data.get('amount')
    description = (data.get('description') or '').strip() or None
    if not user_id or amount is None:
        return jsonify({"error": "user_id e amount são obrigatórios"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "amount inválido"}), 400
    if amount == 0:
        return jsonify({"error": "amount não pode ser zero"}), 400

    type_ = 'deposit' if amount > 0 else 'adjustment'
    new_balance = apply_credit_transaction(user_id, amount, type_, description)
    if new_balance is None:
        return jsonify({"error": "Cliente não encontrado ou erro ao gravar"}), 404
    return jsonify({"user_id": user_id, "balance": round(new_balance, 4)}), 201


@app.route('/admin/credits/<int:user_id>', methods=['GET'])
def admin_get_credit_extract(user_id):
    """Saldo atual + histórico completo de transações de um cliente."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Cliente não encontrado"}), 404
        cur.execute(
            """SELECT id, type, amount, balance_after, description, tokens_input, tokens_output, created_at
               FROM credit_transactions WHERE user_id = %s ORDER BY created_at DESC LIMIT 200""",
            (user_id,)
        )
        transactions = [{
            "id": t[0], "type": t[1], "amount": float(t[2]), "balance_after": float(t[3]),
            "description": t[4], "tokens_input": t[5], "tokens_output": t[6],
            "created_at": t[7].isoformat() if t[7] else None
        } for t in cur.fetchall()]
        conn.close()
        return jsonify({"user_id": user_id, "balance": float(row[0]), "transactions": transactions})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/api/my-credits', methods=['GET'])
def get_my_credits():
    """Saldo + extrato do próprio cliente, identificado pela X-Oraculo-Key —
    mesmo padrão de /api/me e /api/my-area."""
    user_id = resolve_user_from_request()
    if not user_id:
        return jsonify({"error": "Chave de acesso inválida"}), 401
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Cliente não encontrado"}), 404
        cur.execute(
            """SELECT type, amount, balance_after, description, tokens_input, tokens_output, created_at
               FROM credit_transactions WHERE user_id = %s ORDER BY created_at DESC LIMIT 100""",
            (user_id,)
        )
        transactions = [{
            "type": t[0], "amount": float(t[1]), "balance_after": float(t[2]),
            "description": t[3], "tokens_input": t[4], "tokens_output": t[5],
            "created_at": t[6].isoformat() if t[6] else None
        } for t in cur.fetchall()]
        conn.close()
        return jsonify({"balance": float(row[0]), "transactions": transactions})
    except Exception as e:
        if conn: conn.close()
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

    auth_error = authorize_client_area_write(area_id)
    if auth_error:
        return auth_error

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


@app.route('/api/allowed-pages', methods=['GET'])
def get_allowed_pages():
    """Lista pública (sem chave) das páginas liberadas pra clientes — usada
    por access-guard.js em toda página e pelo index.html pra filtrar cards.
    Sem chave de cliente salva no navegador, essa lista nem é consultada
    (admin/uso interno tem acesso total, comportamento de sempre)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"pages": []}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT page FROM client_allowed_pages ORDER BY page")
        pages = [r[0] for r in cur.fetchall()]
        conn.close()
        return jsonify({"pages": pages})
    except Exception as e:
        if conn: conn.close()
        return jsonify({"pages": [], "error": str(e)}), 500


@app.route('/admin/allowed-pages', methods=['PUT'])
def admin_set_allowed_pages():
    """Substitui a lista inteira de páginas liberadas pra clientes — o
    formulário do admin sempre manda a lista completa marcada, mesmo padrão
    de _replace_plan_area_pricing."""
    data = request.get_json()
    pages = data.get('pages') or []
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 500
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM client_allowed_pages")
        for p in pages:
            cur.execute("INSERT INTO client_allowed_pages (page) VALUES (%s) ON CONFLICT DO NOTHING", (p,))
        conn.commit()
        conn.close()
        return jsonify({"pages": pages})
    except Exception as e:
        if conn: conn.rollback(); conn.close()
        return jsonify({"error": str(e)}), 500


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
