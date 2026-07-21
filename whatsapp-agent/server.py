#!/usr/bin/env python3
"""WhatsApp Agent — serviço independente (porta 5005) pra integração com
WhatsApp (conta comum via QR Code, usando a Evolution API como conector).

Roda separado do Oráculo (ai_oraculo_saas) — não importa nem altera nada lá.
Só consome a API do Oráculo quando faz sentido: o bot de resposta automática
(ai_auto_reply_enabled + conta vinculada a uma área) chama /api/chat de lá
via HTTP, nunca lê o banco dele diretamente pra isso.
"""

import base64
import datetime
import hashlib
import mimetypes
import os
import re
import secrets
import threading
import time
import uuid

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

import booking_flow
import connectors.evolution as evolution
from config import DB_CONFIG, ORACULO_API_CONFIG, WHATSAPP_CONFIG, WHATSAPP_MEDIA_CONFIG
from connectors.evolution import EvolutionError
from db_migrations import migrate_if_needed

# Evolution API usa 3 estados (close/connecting/open) — mapeados pro enum de
# whatsapp_accounts.status. 'qr_pending' é setado manualmente por nós logo
# depois de criar a instância (o QR já vem na resposta do create, não
# precisa de polling assíncrono como no conector antigo).
EVOLUTION_STATUS_MAP = {
    "close": "disconnected",
    "connecting": "connecting",
    "open": "connected",
}

app = Flask(__name__, static_folder=None)
CORS(app)

PUBLIC_DIR = "public"

# Documentos/exames capturados do CRM médico — armazenados FORA de PUBLIC_DIR
# (que é servido sem autenticação) e servidos só pela rota autenticada
# /patient-documents/<id>/file. Fallback sensato se whatsapp_agent.media não
# estiver no config.yaml (contas antigas), pra não quebrar o start do serviço.
MEDIA_STORAGE_DIR = os.path.abspath(WHATSAPP_MEDIA_CONFIG.get("storage_dir") or "media_store")
MEDIA_MAX_BYTES = int(WHATSAPP_MEDIA_CONFIG.get("max_bytes") or 10_000_000)
MEDIA_FETCH_TIMEOUT = int(WHATSAPP_MEDIA_CONFIG.get("fetch_timeout") or 30)
MEDIA_ALLOWED_MIMETYPES = set(
    WHATSAPP_MEDIA_CONFIG.get("allowed_mimetypes")
    or ["image/jpeg", "image/png", "image/webp", "application/pdf"]
)


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def slugify(label):
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return s or "conta"


def _session_name(account_id, label):
    # Nome estável da instância na Evolution API — inclui o id pra nunca
    # colidir entre contas com o mesmo rótulo.
    return f"oraculo-{account_id}-{slugify(label)}"


def _phone_from_wa_id(wa_id):
    # wa_id guardado como JID completo (ex: "5537999872331@s.whatsapp.net");
    # a Evolution API quer só o número no campo "number" de /message/sendText.
    return (wa_id or "").split("@")[0]


def resolve_client_from_request():
    """Resolve users.id a partir do header X-Oraculo-Key — mesma chave usada
    em /api/chat no ai_oraculo_saas, validada aqui direto contra a tabela
    users (mesmo Postgres, ai_tutor_db). Nunca aceitar user_id vindo do
    corpo/query de uma requisição do cliente."""
    api_key = request.headers.get("X-Oraculo-Key")
    if not api_key:
        return None
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE api_key = %s AND status = 'active'", (api_key,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def log_event(account_id, event, level="info", detail=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO whatsapp_logs (account_id, level, event, detail) VALUES (%s, %s, %s, %s)",
            (account_id, level, event, psycopg2.extras.Json(detail) if detail else None),
        )
        conn.commit()
    except Exception as e:
        print(f"[log_event] falhou: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Contas
# ---------------------------------------------------------------------------

ACCOUNT_COLUMNS = [
    "id", "label", "connection_type", "phone_number", "status",
    "wa_session_name", "ai_auto_reply_enabled", "last_connected_at", "created_at", "user_id", "area_id",
    "weekly_summary_weekday", "weekly_summary_hour", "secretary_contact_id",
]


def _row_to_account(row):
    d = dict(zip(ACCOUNT_COLUMNS, row))
    for k in ("last_connected_at", "created_at"):
        if d.get(k):
            d[k] = str(d[k])
    return d


def get_accounts(user_id=None):
    # LEFT JOIN com users, areas e whatsapp_client_settings (mesmo ai_tutor_db,
    # mesma conexão — não são bancos separados) só pra trazer o e-mail do
    # cliente, o nome da área e a nomenclatura customizada junto, sem
    # round-trip extra pro frontend.
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(f"a.{c}" for c in ACCOUNT_COLUMNS)
        where = "WHERE a.user_id = %s" if user_id else ""
        params = (user_id,) if user_id else ()
        cur.execute(
            f"""SELECT {cols}, u.email AS client_email, ar.name AS area_name, cs.nomenclature,
                       sec_ct.wa_id AS secretary_wa_id, sec_ct.push_name AS secretary_push_name
                FROM whatsapp_accounts a
                LEFT JOIN users u ON u.id = a.user_id
                LEFT JOIN areas ar ON ar.id = a.area_id
                LEFT JOIN whatsapp_client_settings cs ON cs.user_id = a.user_id
                LEFT JOIN whatsapp_contacts sec_ct ON sec_ct.id = a.secretary_contact_id
                {where}
                ORDER BY a.created_at DESC""",
            params,
        )
        rows = []
        for r in cur.fetchall():
            d = _row_to_account(r[:-5])
            d["client_email"] = r[-5]
            d["area_name"] = r[-4]
            d["nomenclature"] = _merge_nomenclature(r[-3])
            d["secretary_wa_id"] = r[-2]
            d["secretary_push_name"] = r[-1]
            rows.append(d)
        return rows
    finally:
        conn.close()


def get_account(account_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(f"a.{c}" for c in ACCOUNT_COLUMNS)
        cur.execute(
            f"""SELECT {cols}, u.email AS client_email, ar.name AS area_name, cs.nomenclature,
                       sec_ct.wa_id AS secretary_wa_id, sec_ct.push_name AS secretary_push_name
                FROM whatsapp_accounts a
                LEFT JOIN users u ON u.id = a.user_id
                LEFT JOIN areas ar ON ar.id = a.area_id
                LEFT JOIN whatsapp_client_settings cs ON cs.user_id = a.user_id
                LEFT JOIN whatsapp_contacts sec_ct ON sec_ct.id = a.secretary_contact_id
                WHERE a.id = %s""",
            (account_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        d = _row_to_account(row[:-5])
        d["client_email"] = row[-5]
        d["area_name"] = row[-4]
        d["nomenclature"] = _merge_nomenclature(row[-3])
        d["secretary_wa_id"] = row[-2]
        d["secretary_push_name"] = row[-1]
        return d
    finally:
        conn.close()


def get_clients():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, email FROM users ORDER BY email")
        return [{"id": r[0], "email": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Nomenclatura customizável por CLIENTE (não por conta — um cliente pode ter
# mais de uma whatsapp_accounts e a nomenclatura vale pra todas). Hoje só a
# chave "consultant" existe; o formato já é um dict aberto pra dar pra
# acrescentar outras chaves depois sem precisar de migração nova.
# ---------------------------------------------------------------------------

DEFAULT_NOMENCLATURE = {"consultant": {"singular": "Consultor", "plural": "Consultores"}}


def _merge_nomenclature(raw):
    merged = {k: dict(v) for k, v in DEFAULT_NOMENCLATURE.items()}
    if raw:
        for key, val in raw.items():
            if key in merged and isinstance(val, dict):
                merged[key].update({
                    k: v for k, v in val.items()
                    if k in ("singular", "plural") and isinstance(v, str) and v.strip()
                })
    return merged


def get_nomenclature(user_id):
    if not user_id:
        return dict(DEFAULT_NOMENCLATURE)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT nomenclature FROM whatsapp_client_settings WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return _merge_nomenclature(row[0] if row else None)
    finally:
        conn.close()


def set_nomenclature(user_id, data):
    """Só aceita as chaves conhecidas (DEFAULT_NOMENCLATURE) com forma
    singular/plural em texto — ignora qualquer outra coisa vinda do cliente."""
    cleaned = {}
    for key in DEFAULT_NOMENCLATURE:
        val = (data or {}).get(key)
        if not isinstance(val, dict):
            continue
        entry = {}
        for form in ("singular", "plural"):
            v = val.get(form)
            if isinstance(v, str) and v.strip():
                entry[form] = v.strip()[:80]
        if entry:
            cleaned[key] = entry
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO whatsapp_client_settings (user_id, nomenclature, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (user_id) DO UPDATE SET nomenclature = %s, updated_at = NOW()""",
            (user_id, psycopg2.extras.Json(cleaned), psycopg2.extras.Json(cleaned)),
        )
        conn.commit()
    finally:
        conn.close()
    return _merge_nomenclature(cleaned)


def create_account(label, connection_type, user_id=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO whatsapp_accounts (label, connection_type, user_id)
               VALUES (%s, %s, %s) RETURNING id""",
            (label, connection_type, user_id),
        )
        account_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE whatsapp_accounts SET wa_session_name = %s WHERE id = %s",
            (_session_name(account_id, label), account_id),
        )
        conn.commit()
        return account_id
    finally:
        conn.close()


def update_account_client(account_id, user_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_accounts SET user_id = %s WHERE id = %s", (user_id, account_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_area_link(user_id, area_id, account_id):
    # Exclusividade por CLIENTE, não global: duas empresas diferentes no
    # mesmo plano podem vincular números diferentes pra mesma área
    # compartilhada. Por isso o "libera antes de setar" só mexe em contas
    # com o mesmo user_id.
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE whatsapp_accounts SET area_id = NULL WHERE user_id = %s AND area_id = %s",
            (user_id, area_id),
        )
        if account_id:
            cur.execute(
                "UPDATE whatsapp_accounts SET area_id = %s WHERE id = %s AND user_id = %s",
                (area_id, account_id, user_id),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def unlink_area(area_id, user_ids=None):
    # Chamado pelo ai_oraculo_saas quando uma área deixa de estar disponível
    # (arquivada, ou removida do plano) — limpa o vínculo em whatsapp_accounts
    # sem que o Oráculo precise escrever direto nessa tabela.
    conn = _conn()
    try:
        cur = conn.cursor()
        if user_ids:
            cur.execute(
                "UPDATE whatsapp_accounts SET area_id = NULL WHERE area_id = %s AND user_id = ANY(%s)",
                (area_id, user_ids),
            )
        else:
            cur.execute("UPDATE whatsapp_accounts SET area_id = NULL WHERE area_id = %s", (area_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def update_account_status(account_id, status, connected=False):
    conn = _conn()
    try:
        cur = conn.cursor()
        if connected:
            cur.execute(
                "UPDATE whatsapp_accounts SET status = %s, last_connected_at = NOW() WHERE id = %s",
                (status, account_id),
            )
        else:
            cur.execute("UPDATE whatsapp_accounts SET status = %s WHERE id = %s", (status, account_id))
        conn.commit()
    finally:
        conn.close()


def delete_account(account_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM whatsapp_accounts WHERE id = %s", (account_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_qr_session(account_id, qr_base64):
    conn = _conn()
    try:
        cur = conn.cursor()
        now = datetime.datetime.now(datetime.timezone.utc)
        expires = now + datetime.timedelta(seconds=60)
        cur.execute(
            """INSERT INTO whatsapp_sessions (account_id, qr_code_base64, qr_generated_at, qr_expires_at)
               VALUES (%s, %s, %s, %s)""",
            (account_id, qr_base64, now, expires),
        )
        conn.commit()
    finally:
        conn.close()


def mark_session_connected(account_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_sessions SET connected_at = NOW()
               WHERE account_id = %s AND connected_at IS NULL
               ORDER BY id DESC LIMIT 1""",
            (account_id,),
        )
        conn.commit()
    finally:
        conn.close()


def get_account_by_session(session_name):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(ACCOUNT_COLUMNS)} FROM whatsapp_accounts WHERE wa_session_name = %s",
            (session_name,),
        )
        row = cur.fetchone()
        return _row_to_account(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Contatos / conversas / mensagens
# ---------------------------------------------------------------------------

def get_or_create_contact(account_id, wa_id, push_name=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM whatsapp_contacts WHERE account_id = %s AND wa_id = %s",
            (account_id, wa_id),
        )
        row = cur.fetchone()
        if row:
            if push_name:
                cur.execute(
                    "UPDATE whatsapp_contacts SET push_name = %s, last_interaction_at = NOW() WHERE id = %s",
                    (push_name, row[0]),
                )
                conn.commit()
            return row[0]
        cur.execute(
            """INSERT INTO whatsapp_contacts (account_id, wa_id, push_name, last_interaction_at)
               VALUES (%s, %s, %s, NOW()) RETURNING id""",
            (account_id, wa_id, push_name),
        )
        contact_id = cur.fetchone()[0]
        conn.commit()
        return contact_id
    finally:
        conn.close()


def set_account_secretary_contact(account_id, phone):
    """Cadastra/atualiza o contato da secretária da clínica, reaproveitando
    get_or_create_contact — o mesmo mecanismo já usado pro médico virar
    consultor — em vez de guardar um telefone cru sem validação/reuso.
    phone vazio limpa o cadastro (nem toda clínica precisa de secretária)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        if not phone:
            cur.execute("UPDATE whatsapp_accounts SET secretary_contact_id = NULL WHERE id = %s", (account_id,))
            conn.commit()
            return None
        wa_id = f"{phone}@s.whatsapp.net"
        contact_id = get_or_create_contact(account_id, wa_id)
        cur.execute("UPDATE whatsapp_accounts SET secretary_contact_id = %s WHERE id = %s", (contact_id, account_id))
        conn.commit()
        return contact_id
    finally:
        conn.close()


def get_or_create_chat(account_id, contact_id, default_auto_reply=True):
    # default_auto_reply vem de whatsapp_accounts.ai_auto_reply_enabled — é o
    # valor com que TODA conversa nova daquela conta começa (conta de
    # auto-atendimento = default True, conta particular = default False);
    # depois disso o toggle é por conversa, independente da conta.
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM whatsapp_chats WHERE account_id = %s AND contact_id = %s",
            (account_id, contact_id),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            """INSERT INTO whatsapp_chats (account_id, chat_type, contact_id, ai_auto_reply_enabled)
               VALUES (%s, 'contact', %s, %s) RETURNING id""",
            (account_id, contact_id, default_auto_reply),
        )
        chat_id = cur.fetchone()[0]
        conn.commit()
        return chat_id
    finally:
        conn.close()


def get_chat(chat_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT c.id, c.account_id, c.contact_id, ct.wa_id, a.wa_session_name, c.ai_auto_reply_enabled
               FROM whatsapp_chats c
               JOIN whatsapp_contacts ct ON ct.id = c.contact_id
               JOIN whatsapp_accounts a ON a.id = c.account_id
               WHERE c.id = %s""",
            (chat_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip(["id", "account_id", "contact_id", "wa_id", "wa_session_name", "ai_auto_reply_enabled"], row))
    finally:
        conn.close()


def set_chat_auto_reply(chat_id, enabled):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_chats SET ai_auto_reply_enabled = %s WHERE id = %s", (enabled, chat_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_account_auto_reply_default(account_id, enabled):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_accounts SET ai_auto_reply_enabled = %s WHERE id = %s", (enabled, account_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


_MEDIA_PREVIEW = {"image": "🖼 Imagem", "document": "📎 Documento"}


def save_message(chat_id, account_id, direction, body, sender_contact_id=None,
                  wa_message_id=None, message_type="text", file_id=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        status = "sent" if direction == "out" else "delivered"
        cur.execute(
            """INSERT INTO whatsapp_messages
               (chat_id, account_id, wa_message_id, direction, sender_contact_id, message_type, body, file_id, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (chat_id, account_id, wa_message_id, direction, sender_contact_id, message_type, body, file_id, status),
        )
        message_id = cur.fetchone()[0]
        preview = (body or _MEDIA_PREVIEW.get(message_type, ""))[:120]
        if direction == "in":
            cur.execute(
                """UPDATE whatsapp_chats
                   SET last_message_at = NOW(), last_message_preview = %s, unread_count = unread_count + 1
                   WHERE id = %s""",
                (preview, chat_id),
            )
        else:
            cur.execute(
                "UPDATE whatsapp_chats SET last_message_at = NOW(), last_message_preview = %s WHERE id = %s",
                (preview, chat_id),
            )
        conn.commit()
        return message_id
    finally:
        conn.close()


def _set_message_file(message_id, file_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_messages SET file_id = %s WHERE id = %s", (file_id, message_id))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Documentos/exames do paciente (CRM médico) — captura automática de
# imagem/PDF recebido pelo WhatsApp, ver plano em
# vamos-planejar-a-quntiza-o-bubbly-liskov.md
# ---------------------------------------------------------------------------

def _extract_media_info(message):
    """Olha o objeto 'message' cru do webhook e devolve
    {'doc_type','mimetype','caption','file_name'} se for imagem/documento,
    ou None (texto, áudio, sticker etc. — fora de escopo por ora)."""
    for key, doc_type in (("imageMessage", "image"), ("documentMessage", "document")):
        node = message.get(key)
        if node:
            return {
                "doc_type": doc_type,
                "mimetype": node.get("mimetype"),
                "caption": node.get("caption"),
                "file_name": node.get("fileName") or node.get("title"),
            }
    return None


def _recent_appointment_for_contact(account_id, contact_id):
    """Consulta mais próxima no tempo daquele paciente naquela clínica — usada
    só como marcação best-effort de qual consulta o exame provavelmente se
    refere. Sem consulta encontrada, fica NULL, sem problema (o documento é
    ancorado no paciente, não na consulta)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT a.id FROM whatsapp_appointments a
               JOIN whatsapp_consultants c ON c.id = a.consultant_id
               WHERE c.account_id = %s AND a.client_contact_id = %s
                 AND a.status IN ('confirmed', 'pending_consultant', 'completed')
               ORDER BY ABS(EXTRACT(EPOCH FROM (a.scheduled_at - NOW()))) ASC
               LIMIT 1""",
            (account_id, contact_id),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _create_pending_patient_document(account_id, contact_id, message_id, wa_message_id, doc_type, caption, appointment_id):
    """Cria a linha da timeline NA HORA (status pending, sem file_id) — o
    webhook não espera o download terminar. ON CONFLICT DO NOTHING dedupe
    reentrega do mesmo wa_message_id pela Evolution API (acontece na
    prática); devolve None quando já existia (não recria/rebaixa)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO whatsapp_patient_documents
               (contact_id, account_id, appointment_id, message_id, wa_message_id, doc_type, caption, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
               ON CONFLICT (account_id, wa_message_id) WHERE wa_message_id IS NOT NULL DO NOTHING
               RETURNING id""",
            (contact_id, account_id, appointment_id, message_id, wa_message_id, doc_type, caption),
        )
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        conn.close()


def _fail_patient_document(doc_id, reason):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE whatsapp_patient_documents SET status = 'failed', failure_reason = %s WHERE id = %s",
            (reason[:500], doc_id),
        )
        conn.commit()
    finally:
        conn.close()


_EXT_BY_MIMETYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


def _download_patient_media(account, doc_id, contact_id, message_key, media):
    """Roda numa thread separada — TODO o corpo fica dentro de um único
    try/except pra nunca derrubar a thread nem, por consequência, afetar a
    resposta que o webhook já mandou pra Evolution API. Qualquer falha vira
    status='failed' + failure_reason, visível na timeline em vez de sumir."""
    try:
        data = evolution.get_media_base64(account["wa_session_name"], message_key, timeout=MEDIA_FETCH_TIMEOUT)
        raw = base64.b64decode(data.get("base64") or "")
        if not raw:
            _fail_patient_document(doc_id, "Resposta da Evolution API veio sem conteúdo")
            return
        if len(raw) > MEDIA_MAX_BYTES:
            _fail_patient_document(doc_id, f"Arquivo maior que o limite ({len(raw)} bytes)")
            return
        mimetype = data.get("mimetype") or media.get("mimetype") or ""
        if mimetype not in MEDIA_ALLOWED_MIMETYPES:
            _fail_patient_document(doc_id, f"Tipo de arquivo não permitido: {mimetype}")
            return

        checksum = hashlib.sha256(raw).hexdigest()
        ext = _EXT_BY_MIMETYPE.get(mimetype) or mimetypes.guess_extension(mimetype) or ""
        rel_dir = os.path.join(str(account["id"]), str(contact_id))
        rel_path = os.path.join(rel_dir, f"{uuid.uuid4().hex}{ext}")
        full_dir = os.path.join(MEDIA_STORAGE_DIR, rel_dir)
        os.makedirs(full_dir, exist_ok=True)
        full_path = os.path.join(MEDIA_STORAGE_DIR, rel_path)
        with open(full_path, "wb") as f:
            f.write(raw)

        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO whatsapp_files
                   (account_id, mime_type, file_type, original_name, storage_path, size_bytes, checksum_sha256)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (account["id"], mimetype, media["doc_type"], media.get("file_name"), rel_path, len(raw), checksum),
            )
            file_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE whatsapp_patient_documents SET status = 'stored', file_id = %s WHERE id = %s",
                (file_id, doc_id),
            )
            cur.execute(
                "SELECT message_id FROM whatsapp_patient_documents WHERE id = %s", (doc_id,)
            )
            row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
        if row and row[0]:
            _set_message_file(row[0], file_id)
    except EvolutionError as e:
        _fail_patient_document(doc_id, str(e)[:400])
        log_event(account["id"], "patient_media_capture_failed", level="error",
                  detail={"doc_id": doc_id, "error": str(e)})
    except Exception as e:
        _fail_patient_document(doc_id, str(e)[:400])
        log_event(account["id"], "patient_media_capture_failed", level="error",
                  detail={"doc_id": doc_id, "error": str(e)})


def _enqueue_patient_media(account, contact_id, message_id, message_key, media, wa_message_id):
    appointment_id = _recent_appointment_for_contact(account["id"], contact_id)
    doc_id = _create_pending_patient_document(
        account_id=account["id"], contact_id=contact_id, message_id=message_id,
        wa_message_id=wa_message_id, doc_type=media["doc_type"], caption=media.get("caption"),
        appointment_id=appointment_id,
    )
    if doc_id is None:
        return  # já capturado antes (reentrega do webhook) — não duplica
    threading.Thread(target=_download_patient_media, args=(account, doc_id, contact_id, message_key, media),
                      daemon=True).start()


def get_patient_document(doc_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT d.id, d.account_id, d.status, f.storage_path, f.mime_type, f.original_name
               FROM whatsapp_patient_documents d
               LEFT JOIN whatsapp_files f ON f.id = d.file_id
               WHERE d.id = %s""",
            (doc_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "account_id": row[1], "status": row[2],
            "storage_path": row[3], "mime_type": row[4], "original_name": row[5],
        }
    finally:
        conn.close()


def get_patients_with_documents(account_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT ct.id, COALESCE(ct.name, ct.push_name) AS name, ct.wa_id,
                      COUNT(*) AS doc_count, MAX(d.captured_at) AS last_document_at
               FROM whatsapp_patient_documents d
               JOIN whatsapp_contacts ct ON ct.id = d.contact_id
               WHERE d.account_id = %s AND d.hidden = FALSE
               GROUP BY ct.id, ct.name, ct.push_name, ct.wa_id
               ORDER BY MAX(d.captured_at) DESC""",
            (account_id,),
        )
        cols = ["contact_id", "name", "wa_id", "doc_count", "last_document_at"]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["last_document_at"] = d["last_document_at"].isoformat() if d["last_document_at"] else None
            rows.append(d)
        return rows
    finally:
        conn.close()


def get_patients_with_documents_for_consultant(consultant_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT ct.id, COALESCE(ct.name, ct.push_name) AS name, ct.wa_id,
                      COUNT(*) AS doc_count, MAX(d.captured_at) AS last_document_at
               FROM whatsapp_patient_documents d
               JOIN whatsapp_contacts ct ON ct.id = d.contact_id
               WHERE d.hidden = FALSE
                 AND EXISTS (
                     SELECT 1 FROM whatsapp_appointments a
                     WHERE a.consultant_id = %s AND a.client_contact_id = d.contact_id
                 )
               GROUP BY ct.id, ct.name, ct.push_name, ct.wa_id
               ORDER BY MAX(d.captured_at) DESC""",
            (consultant_id,),
        )
        cols = ["contact_id", "name", "wa_id", "doc_count", "last_document_at"]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["last_document_at"] = d["last_document_at"].isoformat() if d["last_document_at"] else None
            rows.append(d)
        return rows
    finally:
        conn.close()


def get_patient_documents_for_contact(contact_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT d.id, d.doc_type, d.caption, d.status, d.failure_reason, d.captured_at,
                      d.appointment_id, f.original_name, f.mime_type, f.size_bytes
               FROM whatsapp_patient_documents d
               LEFT JOIN whatsapp_files f ON f.id = d.file_id
               WHERE d.contact_id = %s AND d.hidden = FALSE
               ORDER BY d.captured_at DESC""",
            (contact_id,),
        )
        cols = ["id", "doc_type", "caption", "status", "failure_reason", "captured_at",
                "appointment_id", "original_name", "mime_type", "size_bytes"]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["captured_at"] = d["captured_at"].isoformat() if d["captured_at"] else None
            rows.append(d)
        return rows
    finally:
        conn.close()


def hide_patient_document(doc_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_patient_documents SET hidden = TRUE WHERE id = %s", (doc_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_chats(account_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT c.id, c.unread_count, c.is_pinned, c.last_message_at, c.last_message_preview,
                      ct.name, ct.push_name, ct.wa_id, c.ai_auto_reply_enabled
               FROM whatsapp_chats c
               JOIN whatsapp_contacts ct ON ct.id = c.contact_id
               WHERE c.account_id = %s AND c.is_archived = FALSE
               ORDER BY c.last_message_at DESC NULLS LAST""",
            (account_id,),
        )
        cols = ["id", "unread_count", "is_pinned", "last_message_at", "last_message_preview",
                "contact_name", "push_name", "wa_id", "ai_auto_reply_enabled"]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            if r.get("last_message_at"):
                r["last_message_at"] = str(r["last_message_at"])
            contact_name = r.pop("contact_name")
            push_name = r.pop("push_name")
            r["display_name"] = contact_name or push_name or r["wa_id"]
        return rows
    finally:
        conn.close()


def list_messages(chat_id, before_id=None, limit=50):
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ["id", "direction", "message_type", "body", "status", "sent_at", "is_ai_generated"]
        if before_id:
            cur.execute(
                f"""SELECT {', '.join(cols)} FROM whatsapp_messages
                    WHERE chat_id = %s AND id < %s ORDER BY id DESC LIMIT %s""",
                (chat_id, before_id, limit),
            )
        else:
            cur.execute(
                f"""SELECT {', '.join(cols)} FROM whatsapp_messages
                    WHERE chat_id = %s ORDER BY id DESC LIMIT %s""",
                (chat_id, limit),
            )
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            if r.get("sent_at"):
                r["sent_at"] = str(r["sent_at"])
        rows.reverse()  # devolve em ordem cronológica (mais antiga primeiro)
        return rows
    finally:
        conn.close()


def mark_chat_read(chat_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_chats SET unread_count = 0 WHERE id = %s", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def get_chat_booking_state(chat_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT booking_state FROM whatsapp_chats WHERE id = %s", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_chat_booking_state(chat_id, state):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE whatsapp_chats SET booking_state = %s WHERE id = %s",
            (psycopg2.extras.Json(state) if state else None, chat_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Agenda de consultores
# ---------------------------------------------------------------------------

def plan_booking_mode(user_id):
    """'none' | 'consultores' | 'crm_medico' — leitura direta em plans (mesmo
    ai_tutor_db, mesma conexão — mesmo padrão já usado em
    get_clients()/_client_api_key()); nunca escrevemos nessa tabela daqui.
    Sem cliente vinculado à conta = 'none'. Consultores e CRM médico são
    mutuamente exclusivos (ver plano): só um dos dois habilita cada recurso."""
    if not user_id:
        return "none"
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT p.booking_mode FROM users u JOIN plans p ON p.id = u.plan_id WHERE u.id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else "none"
    finally:
        conn.close()


CONSULTANT_COLUMNS = [
    "id", "account_id", "contact_id", "name", "context", "slot_duration_minutes",
    "weekly_availability", "reminder_hours_before", "status", "confirmed_at", "created_at",
    "portal_token", "self_availability_enabled",
]


def _row_to_consultant(row):
    d = dict(zip(CONSULTANT_COLUMNS, row))
    for k in ("confirmed_at", "created_at"):
        if d.get(k):
            d[k] = str(d[k])
    return d


def create_consultant(account_id, contact_id, name, context, slot_duration_minutes, weekly_availability, reminder_hours_before):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO whatsapp_consultants
               (account_id, contact_id, name, context, slot_duration_minutes, weekly_availability, reminder_hours_before, portal_token)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (account_id, contact_id, name, context, slot_duration_minutes,
             psycopg2.extras.Json(weekly_availability) if weekly_availability else None, reminder_hours_before,
             secrets.token_hex(24)),
        )
        consultant_id = cur.fetchone()[0]
        conn.commit()
        return consultant_id
    finally:
        conn.close()


def regenerate_portal_token(consultant_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        token = secrets.token_hex(24)
        cur.execute("UPDATE whatsapp_consultants SET portal_token = %s WHERE id = %s", (token, consultant_id))
        conn.commit()
        return token if cur.rowcount else None
    finally:
        conn.close()


def get_consultant_by_portal_token(token):
    """Autenticação do portal do consultor — sem sessão/senha, o token opaco
    (mandado por WhatsApp) É a credencial. Só resolve consultor 'active';
    pending/declined/inactive não têm acesso mesmo com o link antigo em mãos."""
    if not token:
        return None
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(f"c.{col}" for col in CONSULTANT_COLUMNS)
        cur.execute(
            f"""SELECT {cols}, ct.wa_id, a.wa_session_name, a.label
                FROM whatsapp_consultants c
                JOIN whatsapp_contacts ct ON ct.id = c.contact_id
                JOIN whatsapp_accounts a ON a.id = c.account_id
                WHERE c.portal_token = %s AND c.status = 'active'""",
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return None
        d = _row_to_consultant(row[:-3])
        d["wa_id"] = row[-3]
        d["wa_session_name"] = row[-2]
        d["account_label"] = row[-1]
        return d
    finally:
        conn.close()


def get_active_consultant_by_wa_id(account_id, wa_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT c.id, c.portal_token FROM whatsapp_consultants c JOIN whatsapp_contacts ct ON ct.id = c.contact_id
               WHERE c.account_id = %s AND ct.wa_id = %s AND c.status = 'active'""",
            (account_id, wa_id),
        )
        row = cur.fetchone()
        return {"id": row[0], "portal_token": row[1]} if row else None
    finally:
        conn.close()


def get_consultants(account_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(f"c.{col}" for col in CONSULTANT_COLUMNS)
        cur.execute(
            f"""SELECT {cols}, ct.wa_id, ct.push_name
                FROM whatsapp_consultants c JOIN whatsapp_contacts ct ON ct.id = c.contact_id
                WHERE c.account_id = %s ORDER BY c.created_at DESC""",
            (account_id,),
        )
        rows = []
        for r in cur.fetchall():
            d = _row_to_consultant(r[:-2])
            d["wa_id"] = r[-2]
            d["contact_name"] = r[-1]
            rows.append(d)
        return rows
    finally:
        conn.close()


def get_consultant(consultant_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(f"c.{col}" for col in CONSULTANT_COLUMNS)
        cur.execute(
            f"""SELECT {cols}, ct.wa_id, a.wa_session_name
                FROM whatsapp_consultants c
                JOIN whatsapp_contacts ct ON ct.id = c.contact_id
                JOIN whatsapp_accounts a ON a.id = c.account_id
                WHERE c.id = %s""",
            (consultant_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        d = _row_to_consultant(row[:-2])
        d["wa_id"] = row[-2]
        d["wa_session_name"] = row[-1]
        return d
    finally:
        conn.close()


def update_consultant(consultant_id, fields):
    if not fields:
        return False
    conn = _conn()
    try:
        cur = conn.cursor()
        set_parts, values = [], []
        for k, v in fields.items():
            if k == "weekly_availability":
                v = psycopg2.extras.Json(v) if v else None
            set_parts.append(f"{k} = %s")
            values.append(v)
        values.append(consultant_id)
        cur.execute(f"UPDATE whatsapp_consultants SET {', '.join(set_parts)} WHERE id = %s", values)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_consultant(consultant_id):
    """Apaga o consultor. whatsapp_appointments.consultant_id é ON DELETE
    CASCADE — os agendamentos dele somem junto (avisado na UI antes de confirmar)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM whatsapp_consultants WHERE id = %s", (consultant_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_consultant_status(consultant_id, status):
    conn = _conn()
    try:
        cur = conn.cursor()
        if status == "active":
            cur.execute(
                "UPDATE whatsapp_consultants SET status = %s, confirmed_at = NOW() WHERE id = %s",
                (status, consultant_id),
            )
        else:
            cur.execute("UPDATE whatsapp_consultants SET status = %s WHERE id = %s", (status, consultant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_portal_token(consultant_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT portal_token FROM whatsapp_consultants WHERE id = %s", (consultant_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def portal_link(token):
    base = (WHATSAPP_CONFIG.get("portal_base_url") or "http://127.0.0.1:5005").rstrip("/")
    return f"{base}/agenda-consultor?token={token}"


def get_consultant_by_pending_contact(account_id, wa_id):
    """Acha um consultor com confirmação pendente daquele contato — usado no
    webhook pra saber se um clique em botão é resposta ao convite de
    consultor."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT c.id FROM whatsapp_consultants c JOIN whatsapp_contacts ct ON ct.id = c.contact_id
               WHERE c.account_id = %s AND ct.wa_id = %s AND c.status = 'pending_confirmation'""",
            (account_id, wa_id),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_appointments(account_id, upcoming_only=True, date_from=None, date_to=None):
    """date_from/date_to (opcionais, "YYYY-MM-DD") permitem pedir uma janela
    explícita (ex.: a semana corrente pro painel da secretária) — quando
    informados, prevalecem sobre upcoming_only. date_to é exclusivo (< , não
    <=). Interpretados como meia-noite em America/Sao_Paulo (não a timezone
    da sessão do Postgres) — mesmo cuidado de fuso já usado no resto do
    arquivo (.astimezone(booking_flow.LOCAL_TZ))."""
    conn = _conn()
    try:
        cur = conn.cursor()
        where_parts = ["c.account_id = %s"]
        params = [account_id]
        if date_from:
            where_parts.append("a2.scheduled_at >= (%s::date)::timestamp AT TIME ZONE 'America/Sao_Paulo'")
            params.append(date_from)
        if date_to:
            where_parts.append("a2.scheduled_at < (%s::date)::timestamp AT TIME ZONE 'America/Sao_Paulo'")
            params.append(date_to)
        if not date_from and not date_to and upcoming_only:
            where_parts.append("a2.scheduled_at >= NOW()")
        where_sql = " AND ".join(where_parts)
        cur.execute(
            f"""SELECT a2.id, a2.consultant_id, c.name, a2.client_contact_id, ct.push_name, ct.wa_id,
                       a2.scheduled_at, a2.duration_minutes, a2.status, a2.subject
                FROM whatsapp_appointments a2
                JOIN whatsapp_consultants c ON c.id = a2.consultant_id
                JOIN whatsapp_contacts ct ON ct.id = a2.client_contact_id
                WHERE {where_sql}
                ORDER BY a2.scheduled_at""",
            params,
        )
        cols = ["id", "consultant_id", "consultant_name", "client_contact_id", "client_name", "client_wa_id",
                "scheduled_at", "duration_minutes", "status", "subject"]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            # .astimezone() converte pra America/Sao_Paulo independente de
            # qual offset o psycopg2 devolveu — evita horário errado na UI.
            r["scheduled_at"] = r["scheduled_at"].astimezone(booking_flow.LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")
        return rows
    finally:
        conn.close()


def cancel_appointment(appointment_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_appointments SET status = 'cancelled' WHERE id = %s", (appointment_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_consultant_appointments(consultant_id):
    """Pro portal do próprio consultor: pedidos aguardando confirmação
    ('pending_consultant', vindos do self-service do cliente) + agenda futura
    já confirmada + um histórico curto (últimos concluídos/cancelados/
    passados), separado do get_appointments admin (que é por CONTA, não por
    consultor)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cols_sql = "a.id, a.client_contact_id, ct.push_name, ct.wa_id, a.scheduled_at, a.duration_minutes, a.status, a.subject"
        cols = ["id", "client_contact_id", "client_name", "client_wa_id", "scheduled_at", "duration_minutes", "status", "subject"]

        cur.execute(
            f"""SELECT {cols_sql} FROM whatsapp_appointments a JOIN whatsapp_contacts ct ON ct.id = a.client_contact_id
                WHERE a.consultant_id = %s AND a.status = 'pending_consultant' AND a.scheduled_at >= NOW()
                ORDER BY a.scheduled_at""",
            (consultant_id,),
        )
        pending = [dict(zip(cols, r)) for r in cur.fetchall()]

        cur.execute(
            f"""SELECT {cols_sql} FROM whatsapp_appointments a JOIN whatsapp_contacts ct ON ct.id = a.client_contact_id
                WHERE a.consultant_id = %s AND a.status = 'confirmed' AND a.scheduled_at >= NOW()
                ORDER BY a.scheduled_at""",
            (consultant_id,),
        )
        upcoming = [dict(zip(cols, r)) for r in cur.fetchall()]

        cur.execute(
            f"""SELECT {cols_sql} FROM whatsapp_appointments a JOIN whatsapp_contacts ct ON ct.id = a.client_contact_id
                WHERE a.consultant_id = %s AND (a.scheduled_at < NOW() OR a.status NOT IN ('confirmed', 'pending_consultant'))
                ORDER BY a.scheduled_at DESC LIMIT 10""",
            (consultant_id,),
        )
        history = [dict(zip(cols, r)) for r in cur.fetchall()]

        for lst in (pending, upcoming, history):
            for r in lst:
                r["scheduled_at"] = r["scheduled_at"].astimezone(booking_flow.LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")
        return pending, upcoming, history
    finally:
        conn.close()


def mark_appointment_completed(appointment_id):
    """Único ponto de entrada pra 'a consulta aconteceu' — sempre um clique
    manual da secretária, nunca automático (não existe job que 'adivinha'
    comparecimento). Ao completar, nasce um item de checklist por etapa ativa
    do template da clínica (ON CONFLICT DO NOTHING pra ser seguro se chamado
    2x)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_appointments SET status = 'completed', completed_at = NOW()
               WHERE id = %s AND status IN ('confirmed', 'pending_consultant')""",
            (appointment_id,),
        )
        if cur.rowcount == 0:
            conn.commit()
            return False
        cur.execute(
            """SELECT c.account_id FROM whatsapp_appointments a
               JOIN whatsapp_consultants c ON c.id = a.consultant_id
               WHERE a.id = %s""",
            (appointment_id,),
        )
        account_id = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM whatsapp_checklist_templates WHERE account_id = %s AND active",
            (account_id,),
        )
        for (template_id,) in cur.fetchall():
            cur.execute(
                """INSERT INTO whatsapp_checklist_items (appointment_id, template_id)
                   VALUES (%s, %s) ON CONFLICT (appointment_id, template_id) DO NOTHING""",
                (appointment_id, template_id),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def get_checklist_template(account_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, step_key, label, sort_order, notify_patient, notify_consultant, notify_secretary,
                      auto_message_template
               FROM whatsapp_checklist_templates
               WHERE account_id = %s AND active
               ORDER BY sort_order""",
            (account_id,),
        )
        cols = ["id", "step_key", "label", "sort_order", "notify_patient", "notify_consultant", "notify_secretary",
                "auto_message_template"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def set_checklist_template(account_id, steps):
    """Substitui a configuração de etapas da clínica. Faz UPSERT por id (não
    DELETE+INSERT) porque whatsapp_checklist_items.template_id referencia essa
    tabela com ON DELETE CASCADE — apagar e recriar a linha do template
    apagaria junto o progresso de checklist já registrado em consultas
    passadas. Etapa removida pela clínica só vira active=FALSE (soft delete),
    continua existindo pra não quebrar o histórico já criado."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, step_key FROM whatsapp_checklist_templates WHERE account_id = %s",
            (account_id,),
        )
        existing = {r[0]: r[1] for r in cur.fetchall()}
        used_keys = set(existing.values())
        kept_ids = set()

        for idx, step in enumerate(steps or []):
            if not isinstance(step, dict):
                continue
            label = (step.get("label") or "").strip()[:150]
            if not label:
                continue
            auto_template = (step.get("auto_message_template") or "").strip()[:2000] or None
            notify_patient = bool(step.get("notify_patient")) and bool(auto_template)
            notify_consultant = bool(step.get("notify_consultant")) and bool(auto_template)
            notify_secretary = bool(step.get("notify_secretary")) and bool(auto_template)

            step_id = step.get("id")
            if isinstance(step_id, int) and step_id in existing:
                cur.execute(
                    """UPDATE whatsapp_checklist_templates
                       SET label = %s, sort_order = %s, notify_patient = %s, notify_consultant = %s,
                           notify_secretary = %s, auto_message_template = %s, active = TRUE
                       WHERE id = %s AND account_id = %s""",
                    (label, idx, notify_patient, notify_consultant, notify_secretary, auto_template, step_id, account_id),
                )
                kept_ids.add(step_id)
            else:
                key = slugify(label)[:45] or f"etapa-{idx}"
                base_key, n = key, 2
                while key in used_keys:
                    key = f"{base_key}-{n}"[:50]
                    n += 1
                used_keys.add(key)
                cur.execute(
                    """INSERT INTO whatsapp_checklist_templates
                       (account_id, step_key, label, sort_order, notify_patient, notify_consultant,
                        notify_secretary, auto_message_template)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (account_id, key, label, idx, notify_patient, notify_consultant, notify_secretary, auto_template),
                )
                kept_ids.add(cur.fetchone()[0])

        removed_ids = list(set(existing) - kept_ids)
        if removed_ids:
            cur.execute(
                "UPDATE whatsapp_checklist_templates SET active = FALSE WHERE id = ANY(%s)",
                (removed_ids,),
            )
        conn.commit()
    finally:
        conn.close()
    return get_checklist_template(account_id)


def get_checklist_items_for_appointment(appointment_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT i.id, i.status, i.done_at,
                      i.auto_message_sent_patient_at, i.auto_message_sent_consultant_at, i.auto_message_sent_secretary_at,
                      t.label, t.sort_order, t.notify_patient, t.notify_consultant, t.notify_secretary
               FROM whatsapp_checklist_items i
               JOIN whatsapp_checklist_templates t ON t.id = i.template_id
               WHERE i.appointment_id = %s
               ORDER BY t.sort_order""",
            (appointment_id,),
        )
        cols = ["id", "status", "done_at",
                "auto_message_sent_patient_at", "auto_message_sent_consultant_at", "auto_message_sent_secretary_at",
                "label", "sort_order", "notify_patient", "notify_consultant", "notify_secretary"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def get_checklist_items_for_account(account_id, status=None):
    """Board agregado pro painel da secretária — todos os itens de checklist
    de todos os médicos da conta, com dados do paciente/médico/consulta pra
    render direto sem chamada extra por linha."""
    conn = _conn()
    try:
        cur = conn.cursor()
        where_status = "AND i.status = %s" if status else ""
        params = [account_id] + ([status] if status else [])
        cur.execute(
            f"""SELECT i.id, i.status, i.done_at, t.label, t.notify_patient, t.notify_consultant, t.notify_secretary,
                       a.id, a.scheduled_at, a.subject,
                       con.id, con.name,
                       ct.id, ct.push_name, ct.wa_id
                FROM whatsapp_checklist_items i
                JOIN whatsapp_checklist_templates t ON t.id = i.template_id
                JOIN whatsapp_appointments a ON a.id = i.appointment_id
                JOIN whatsapp_consultants con ON con.id = a.consultant_id
                JOIN whatsapp_contacts ct ON ct.id = a.client_contact_id
                WHERE con.account_id = %s {where_status}
                ORDER BY ct.push_name, a.scheduled_at DESC, t.sort_order""",
            params,
        )
        cols = ["id", "status", "done_at", "label", "notify_patient", "notify_consultant", "notify_secretary",
                "appointment_id", "scheduled_at", "subject",
                "consultant_id", "consultant_name",
                "client_contact_id", "client_name", "client_wa_id"]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r["scheduled_at"] = r["scheduled_at"].astimezone(booking_flow.LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")
        return rows
    finally:
        conn.close()


def mark_checklist_item(item_id, new_status):
    """Atualiza o status do item e devolve os dados necessários pra decidir
    (e disparar) a mensagem automática — não manda a mensagem aqui dentro,
    quem chama decide, porque essa função só mexe no banco."""
    conn = _conn()
    try:
        cur = conn.cursor()
        done_at_sql = "NOW()" if new_status == "done" else "NULL"
        cur.execute(
            f"UPDATE whatsapp_checklist_items SET status = %s, done_at = {done_at_sql} WHERE id = %s",
            (new_status, item_id),
        )
        if cur.rowcount == 0:
            conn.commit()
            return None
        cur.execute(
            """SELECT i.auto_message_sent_patient_at, i.auto_message_sent_consultant_at, i.auto_message_sent_secretary_at,
                      t.notify_patient, t.notify_consultant, t.notify_secretary, t.auto_message_template,
                      a.scheduled_at, a.subject, acc.wa_session_name, acc.label,
                      con.name, ct.wa_id, ct.push_name, cons_ct.wa_id, sec_ct.wa_id
               FROM whatsapp_checklist_items i
               JOIN whatsapp_checklist_templates t ON t.id = i.template_id
               JOIN whatsapp_appointments a ON a.id = i.appointment_id
               JOIN whatsapp_consultants con ON con.id = a.consultant_id
               JOIN whatsapp_accounts acc ON acc.id = con.account_id
               JOIN whatsapp_contacts ct ON ct.id = a.client_contact_id
               JOIN whatsapp_contacts cons_ct ON cons_ct.id = con.contact_id
               LEFT JOIN whatsapp_contacts sec_ct ON sec_ct.id = acc.secretary_contact_id
               WHERE i.id = %s""",
            (item_id,),
        )
        row = cur.fetchone()
        conn.commit()
        cols = ["auto_message_sent_patient_at", "auto_message_sent_consultant_at", "auto_message_sent_secretary_at",
                "notify_patient", "notify_consultant", "notify_secretary", "auto_message_template",
                "scheduled_at", "subject", "wa_session_name", "account_label",
                "consultant_name", "client_wa_id", "client_push_name", "consultant_wa_id", "secretary_wa_id"]
        return dict(zip(cols, row)) if row else None
    finally:
        conn.close()


_CHECKLIST_RECIPIENT_COLUMNS = {
    "patient": "auto_message_sent_patient_at",
    "consultant": "auto_message_sent_consultant_at",
    "secretary": "auto_message_sent_secretary_at",
}


def _mark_checklist_auto_message_sent(item_id, recipient):
    column = _CHECKLIST_RECIPIENT_COLUMNS[recipient]
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE whatsapp_checklist_items SET {column} = NOW() WHERE id = %s", (item_id,))
        conn.commit()
    finally:
        conn.close()


def _render_checklist_message(template_text, ctx):
    """Substitui só os placeholders conhecidos via str.replace simples —
    nunca str.format/template engine: o texto vem digitado livremente pela
    secretária e não pode virar vetor de KeyError nem de execução."""
    text = template_text
    for key, value in ctx.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def _due_reminders():
    """Agendamentos confirmados cujo horário de lembrete (scheduled_at menos
    reminder_hours_before do consultor) já chegou, mas o lembrete ainda não
    foi mandado."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT a.id, a.scheduled_at, con.name, con.reminder_hours_before, acc.wa_session_name,
                      client_ct.wa_id, client_ct.push_name, cons_ct.wa_id
               FROM whatsapp_appointments a
               JOIN whatsapp_consultants con ON con.id = a.consultant_id
               JOIN whatsapp_accounts acc ON acc.id = con.account_id
               JOIN whatsapp_contacts client_ct ON client_ct.id = a.client_contact_id
               JOIN whatsapp_contacts cons_ct ON cons_ct.id = con.contact_id
               WHERE a.status = 'confirmed' AND a.reminder_sent_at IS NULL
                 AND a.scheduled_at > NOW()
                 AND a.scheduled_at <= NOW() + make_interval(hours => con.reminder_hours_before)"""
        )
        cols = ["id", "scheduled_at", "consultant_name", "reminder_hours_before", "wa_session_name",
                "client_wa_id", "client_push_name", "consultant_wa_id"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _mark_reminder_sent(appointment_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_appointments SET reminder_sent_at = NOW() WHERE id = %s", (appointment_id,))
        conn.commit()
    finally:
        conn.close()


def _due_weekly_consultants():
    """Consultores ativos que devem receber o resumo semanal: cada CONTA
    (clínica) configura seu próprio dia/hora (whatsapp_accounts.
    weekly_summary_weekday/hour, default segunda 07h — mesmo valor pra todos
    os médicos daquela conta). 'most_recent_occurrence' é a última vez que
    esse dia/hora aconteceu (hoje, se já passou, senão na semana anterior);
    fica devido se ainda não foi mandado nada desde essa ocorrência —  mesmo
    raciocínio de idempotência de _due_reminders, só que por semana em vez de
    por agendamento. Não trava numa janela de tempo: se o serviço ficar fora
    do ar na hora exata, manda assim que voltar."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT con.id, con.name, acc.wa_session_name, acc.label, ct.wa_id
               FROM whatsapp_consultants con
               JOIN whatsapp_accounts acc ON acc.id = con.account_id
               JOIN whatsapp_contacts ct ON ct.id = con.contact_id
               CROSS JOIN LATERAL (
                   SELECT date_trunc('day', NOW() AT TIME ZONE 'America/Sao_Paulo')
                          - make_interval(days => ((EXTRACT(DOW FROM (NOW() AT TIME ZONE 'America/Sao_Paulo'))::int
                                                     - acc.weekly_summary_weekday + 7) % 7))
                          + make_interval(hours => acc.weekly_summary_hour) AS most_recent_occurrence
               ) occ
               WHERE con.status = 'active'
                 AND (NOW() AT TIME ZONE 'America/Sao_Paulo') >= occ.most_recent_occurrence
                 AND (con.last_weekly_summary_sent_at IS NULL
                      OR (con.last_weekly_summary_sent_at AT TIME ZONE 'America/Sao_Paulo') < occ.most_recent_occurrence)"""
        )
        cols = ["id", "name", "wa_session_name", "account_label", "wa_id"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _mark_weekly_summary_sent(consultant_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE whatsapp_consultants SET last_weekly_summary_sent_at = NOW() WHERE id = %s",
            (consultant_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _reminder_loop():
    """Único processo em background do whatsapp-agent — não existia nenhum
    antes desta feature (tudo mais é disparado sob demanda por webhook).
    Polling simples (sem dependência nova tipo APScheduler): a cada 5min,
    manda lembrete pro cliente e pro consultor de agendamentos cujo horário
    de aviso chegou, e (bloco separado, próprio try/except pra um não
    derrubar o outro) o resumo semanal de agenda pro médico."""
    while True:
        try:
            for appt in _due_reminders():
                # scheduled_at volta do banco com tzinfo (TIMESTAMPTZ) mas não
                # necessariamente já em America/Sao_Paulo — .astimezone()
                # converte certo independente de qual offset o psycopg2 deu.
                when = appt["scheduled_at"].astimezone(booking_flow.LOCAL_TZ).strftime("%d/%m às %H:%M")
                client_phone = _phone_from_wa_id(appt["client_wa_id"])
                consultant_phone = _phone_from_wa_id(appt["consultant_wa_id"])
                try:
                    evolution.send_text(appt["wa_session_name"], client_phone,
                                         f"Lembrete: você tem um agendamento com {appt['consultant_name']} em {when}.")
                    evolution.send_text(appt["wa_session_name"], consultant_phone,
                                         f"Lembrete: você tem um agendamento com {appt['client_push_name'] or client_phone} em {when}.")
                except EvolutionError as e:
                    log_event(None, "reminder_send_failed", level="error",
                              detail={"appointment_id": appt["id"], "error": str(e)})
                _mark_reminder_sent(appt["id"])
        except Exception as e:
            log_event(None, "reminder_loop_error", level="error", detail={"error": str(e)})

        try:
            for consultant in _due_weekly_consultants():
                limit = (datetime.datetime.now(booking_flow.LOCAL_TZ) + datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
                _, upcoming, _ = get_consultant_appointments(consultant["id"])
                week_appts = [a for a in upcoming if a["scheduled_at"] <= limit]
                if week_appts:
                    linhas = "\n".join(
                        f"- {a['scheduled_at'][8:10]}/{a['scheduled_at'][5:7]} {a['scheduled_at'][11:16]} · "
                        f"{a['client_name'] or a['client_wa_id']}"
                        for a in week_appts[:20]
                    )
                    texto = f"Resumo da sua semana ({consultant['account_label']}):\n{linhas}"
                else:
                    texto = (f"Resumo da sua semana ({consultant['account_label']}): "
                             f"nenhuma consulta confirmada nos próximos 7 dias.")
                try:
                    evolution.send_text(consultant["wa_session_name"], _phone_from_wa_id(consultant["wa_id"]), texto)
                except EvolutionError as e:
                    log_event(None, "weekly_summary_send_failed", level="error",
                              detail={"consultant_id": consultant["id"], "error": str(e)})
                _mark_weekly_summary_sent(consultant["id"])
        except Exception as e:
            log_event(None, "weekly_summary_loop_error", level="error", detail={"error": str(e)})

        time.sleep(300)


# ---------------------------------------------------------------------------
# Rotas — página própria
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.route("/docs")
def api_docs():
    return send_from_directory(PUBLIC_DIR, "api-docs.html")


@app.route("/agenda-consultor")
def consultant_portal_page():
    return send_from_directory(PUBLIC_DIR, "consultant-portal.html")


@app.route("/area-cliente")
def client_portal_page():
    return send_from_directory(PUBLIC_DIR, "area-cliente.html")


@app.route("/painel-secretaria")
def secretary_panel_page():
    return send_from_directory(PUBLIC_DIR, "painel-secretaria.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "whatsapp-agent"})


# ---------------------------------------------------------------------------
# Rotas — API de contas
# ---------------------------------------------------------------------------

@app.route("/api/whatsapp/accounts", methods=["GET"])
def api_list_accounts():
    user_id = request.args.get("user_id", type=int)
    return jsonify({"accounts": get_accounts(user_id=user_id)})


@app.route("/api/whatsapp/clients", methods=["GET"])
def api_list_clients():
    return jsonify({"clients": get_clients()})


@app.route("/api/whatsapp/accounts", methods=["POST"])
def api_create_account():
    data = request.json or {}
    label = (data.get("label") or "").strip()
    connection_type = data.get("connection_type") or "qrcode"
    user_id = data.get("user_id") or None
    if not label:
        return jsonify({"ok": False, "message": "Label é obrigatório"}), 400
    if connection_type not in ("qrcode", "business_api"):
        return jsonify({"ok": False, "message": "connection_type inválido"}), 400
    if connection_type == "business_api":
        return jsonify({"ok": False, "message": "Business API ainda não implementado nesta primeira versão — use QR Code."}), 400

    account_id = create_account(label, connection_type, user_id=user_id)
    log_event(account_id, "account_created", detail={"label": label, "connection_type": connection_type, "user_id": user_id})
    return jsonify({"ok": True, "id": account_id}), 201


@app.route("/api/whatsapp/accounts/<int:account_id>", methods=["PATCH"])
def api_update_account(account_id):
    if not get_account(account_id):
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    data = request.json or {}
    if "user_id" not in data and "ai_auto_reply_enabled" not in data:
        return jsonify({"ok": False, "message": "Nada para atualizar"}), 400
    if "user_id" in data:
        update_account_client(account_id, data["user_id"] or None)
    if "ai_auto_reply_enabled" in data:
        update_account_auto_reply_default(account_id, bool(data["ai_auto_reply_enabled"]))
    return jsonify({"ok": True})


@app.route("/api/whatsapp/area-link", methods=["PUT"])
def api_set_area_link():
    """Vincula (ou desvincula, account_id=null) uma área a uma conexão de um
    cliente específico. Chamado pelo ai_oraculo_saas (cadastro de clientes),
    nunca escrito direto na tabela pelo outro serviço."""
    data = request.json or {}
    user_id = data.get("user_id")
    area_id = data.get("area_id")
    account_id = data.get("account_id") or None
    if not user_id or not area_id:
        return jsonify({"ok": False, "message": "user_id e area_id são obrigatórios"}), 400
    if account_id and not get_account(account_id):
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    set_area_link(user_id, area_id, account_id)
    log_event(account_id, "area_link_set", detail={"user_id": user_id, "area_id": area_id})
    return jsonify({"ok": True})


@app.route("/api/whatsapp/accounts/unlink-area", methods=["POST"])
def api_unlink_area():
    """Limpeza em cascata: chamado pelo ai_oraculo_saas quando uma área é
    arquivada (sem user_ids, limpa em qualquer cliente) ou removida de um
    plano específico (com user_ids, só limpa quem estava naquele plano)."""
    data = request.json or {}
    area_id = data.get("area_id")
    user_ids = data.get("user_ids") or None
    if not area_id:
        return jsonify({"ok": False, "message": "area_id é obrigatório"}), 400
    count = unlink_area(area_id, user_ids=user_ids)
    log_event(None, "area_unlinked", detail={"area_id": area_id, "user_ids": user_ids, "accounts_affected": count})
    return jsonify({"ok": True, "accounts_affected": count})


@app.route("/api/whatsapp/accounts/<int:account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    if account["connection_type"] == "qrcode":
        try:
            evolution.logout(account["wa_session_name"])
        except EvolutionError:
            pass
        try:
            evolution.delete_instance(account["wa_session_name"])
        except EvolutionError:
            pass
    delete_account(account_id)
    return jsonify({"ok": True})


@app.route("/api/whatsapp/accounts/<int:account_id>/connect", methods=["POST"])
def api_connect_account(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    if account["connection_type"] != "qrcode":
        return jsonify({"ok": False, "message": "Só contas QR Code usam este fluxo"}), 400

    instance_name = account["wa_session_name"]
    webhook_base = (WHATSAPP_CONFIG.get("webhook_base_url") or "http://127.0.0.1:5005").rstrip("/")
    webhook_url = f"{webhook_base}/webhooks/evolution"

    try:
        result = evolution.create_instance(instance_name)
        qr_data = (result or {}).get("qrcode") or {}
        if result.get("already_exists"):
            # instância já existia (reconectar depois de logout) — busca QR novo
            qr_data = evolution.get_qr(instance_name)
        evolution.set_webhook(instance_name, webhook_url)
    except EvolutionError as e:
        update_account_status(account_id, "error")
        log_event(account_id, "connect_failed", level="error", detail={"error": str(e)})
        return jsonify({"ok": False, "message": str(e)}), 502

    qr_base64 = qr_data.get("base64")
    if not qr_base64:
        # já pode estar conectado (sessão anterior ainda válida)
        try:
            state = evolution.connection_state(instance_name)
        except EvolutionError as e:
            update_account_status(account_id, "error")
            return jsonify({"ok": False, "message": str(e)}), 502
        if state == "open":
            update_account_status(account_id, "connected", connected=True)
            log_event(account_id, "reconnected_without_qr")
            return jsonify({"ok": True, "status": "connected"})
        update_account_status(account_id, "error")
        return jsonify({"ok": False, "message": "Evolution API não retornou QR nem estado conectado"}), 502

    save_qr_session(account_id, qr_base64)
    update_account_status(account_id, "qr_pending")
    log_event(account_id, "qr_generated")
    return jsonify({"ok": True, "status": "qr_pending", "qr_base64": qr_base64})


@app.route("/api/whatsapp/accounts/<int:account_id>/status", methods=["GET"])
def api_account_status(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404

    if account["connection_type"] == "qrcode" and account["status"] in ("qr_pending", "connecting", "connected"):
        try:
            evo_state = evolution.connection_state(account["wa_session_name"])
        except EvolutionError as e:
            return jsonify({"ok": True, "account": account, "warning": str(e)})

        our_status = EVOLUTION_STATUS_MAP.get(evo_state, "error")
        if our_status == "connected" and account["status"] != "connected":
            update_account_status(account_id, "connected", connected=True)
            mark_session_connected(account_id)
            log_event(account_id, "connected")
            account = get_account(account_id)
        elif our_status == "disconnected" and account["status"] == "connected":
            update_account_status(account_id, "disconnected")
            log_event(account_id, "disconnected", level="warning")
            account = get_account(account_id)

    return jsonify({"ok": True, "account": account})


@app.route("/api/whatsapp/accounts/<int:account_id>/disconnect", methods=["POST"])
def api_disconnect_account(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    try:
        evolution.logout(account["wa_session_name"])
    except EvolutionError as e:
        return jsonify({"ok": False, "message": str(e)}), 502
    update_account_status(account_id, "disconnected")
    log_event(account_id, "disconnected_manual")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Rotas — conversas e mensagens
# ---------------------------------------------------------------------------

@app.route("/api/whatsapp/accounts/<int:account_id>/chats", methods=["GET"])
def api_list_chats(account_id):
    if not get_account(account_id):
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    return jsonify({"chats": list_chats(account_id)})


@app.route("/api/whatsapp/chats/<int:chat_id>", methods=["PATCH"])
def api_update_chat(chat_id):
    if not get_chat(chat_id):
        return jsonify({"ok": False, "message": "Conversa não encontrada"}), 404
    data = request.json or {}
    if "ai_auto_reply_enabled" not in data:
        return jsonify({"ok": False, "message": "Nada para atualizar"}), 400
    set_chat_auto_reply(chat_id, bool(data["ai_auto_reply_enabled"]))
    return jsonify({"ok": True})


@app.route("/api/whatsapp/chats/<int:chat_id>/messages", methods=["GET"])
def api_list_messages(chat_id):
    before_id = request.args.get("before_id", type=int)
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"messages": list_messages(chat_id, before_id=before_id, limit=limit)})


@app.route("/api/whatsapp/chats/<int:chat_id>/messages", methods=["POST"])
def api_send_message(chat_id):
    data = request.json or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "message": "Texto é obrigatório"}), 400

    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"ok": False, "message": "Conversa não encontrada"}), 404

    try:
        result = evolution.send_text(chat["wa_session_name"], _phone_from_wa_id(chat["wa_id"]), text)
    except EvolutionError as e:
        return jsonify({"ok": False, "message": str(e)}), 502

    wa_message_id = ((result or {}).get("key") or {}).get("id")
    message_id = save_message(chat_id, chat["account_id"], "out", text, wa_message_id=wa_message_id)
    return jsonify({"ok": True, "id": message_id})


@app.route("/api/whatsapp/accounts/<int:account_id>/chats/start", methods=["POST"])
def api_start_chat(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404

    data = request.json or {}
    phone = re.sub(r"\D", "", data.get("phone") or "")
    text = (data.get("text") or "").strip()
    if not phone:
        return jsonify({"ok": False, "message": "Telefone é obrigatório (só números, com DDI, ex: 5511999999999)"}), 400
    if not text:
        return jsonify({"ok": False, "message": "Texto é obrigatório"}), 400

    wa_id = f"{phone}@s.whatsapp.net"
    contact_id = get_or_create_contact(account_id, wa_id)
    chat_id = get_or_create_chat(account_id, contact_id, default_auto_reply=account.get("ai_auto_reply_enabled", True))

    try:
        result = evolution.send_text(account["wa_session_name"], phone, text)
    except EvolutionError as e:
        return jsonify({"ok": False, "message": str(e)}), 502

    wa_message_id = ((result or {}).get("key") or {}).get("id")
    save_message(chat_id, account_id, "out", text, wa_message_id=wa_message_id)
    return jsonify({"ok": True, "chat_id": chat_id, "wa_message_id": wa_message_id})


@app.route("/api/whatsapp/chats/<int:chat_id>/read", methods=["POST"])
def api_mark_chat_read(chat_id):
    mark_chat_read(chat_id)
    return jsonify({"ok": True})


def _send_consultant_invite_message(consultant):
    """Texto puro, não botão (send_buttons) — o WhatsApp Web/Desktop não sabe
    exibir o formato de botão nativo que a Evolution API usa (viewOnceMessage/
    nativeFlowMessage) e mostra 'Não foi possível carregar a mensagem' pra
    quem recebe por lá. O webhook já aceita SIM/NÃO digitado (era o fallback
    de texto do botão, agora é o único caminho)."""
    account = get_account(consultant["account_id"])
    term = get_nomenclature(account.get("user_id") if account else None)["consultant"]["singular"].lower()
    evolution.send_text(
        consultant["wa_session_name"],
        _phone_from_wa_id(consultant["wa_id"]),
        f"Você foi cadastrado como {term} ({consultant['name']}) pra receber agendamentos por aqui. "
        f"Confirma o cadastro? Responda SIM ou NÃO.",
    )


def _send_consultant_confirmation(consultant_id):
    """Manda o convite com botões Sim/Não pro contato confirmar que aceita
    virar consultor — sem isso o status nunca sai de 'pending_confirmation'
    e ele não aparece nas opções do fluxo de agendamento. Roda em thread
    separada, erro só loga."""
    consultant = get_consultant(consultant_id)
    if not consultant:
        return
    try:
        _send_consultant_invite_message(consultant)
    except EvolutionError as e:
        log_event(consultant["account_id"], "consultant_invite_failed", level="error",
                   detail={"error": str(e), "consultant_id": consultant_id})


@app.route("/api/whatsapp/accounts/<int:account_id>/consultants", methods=["GET"])
def api_list_consultants(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    if plan_booking_mode(account.get("user_id")) == "none":
        return jsonify({"ok": False, "message": "Agenda não está ativada no plano deste cliente."}), 403
    return jsonify({"consultants": get_consultants(account_id)})


@app.route("/api/whatsapp/accounts/<int:account_id>/consultants", methods=["POST"])
def api_create_consultant(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    if plan_booking_mode(account.get("user_id")) == "none":
        return jsonify({"ok": False, "message": "Agenda não está ativada no plano deste cliente."}), 403

    data = request.json or {}
    phone = re.sub(r"\D", "", data.get("phone") or "")
    name = (data.get("name") or "").strip()
    if not phone or not name:
        return jsonify({"ok": False, "message": "Telefone e nome são obrigatórios"}), 400

    wa_id = f"{phone}@s.whatsapp.net"
    contact_id = get_or_create_contact(account_id, wa_id)
    try:
        consultant_id = create_consultant(
            account_id, contact_id, name,
            data.get("context"),
            int(data.get("slot_duration_minutes") or 30),
            data.get("weekly_availability"),
            int(data.get("reminder_hours_before") or 2),
        )
    except psycopg2.errors.UniqueViolation:
        return jsonify({"ok": False, "message": "Esse contato já é consultor desta conta"}), 409

    threading.Thread(target=_send_consultant_confirmation, args=(consultant_id,), daemon=True).start()
    return jsonify({"ok": True, "id": consultant_id}), 201


@app.route("/api/whatsapp/consultants/<int:consultant_id>", methods=["PATCH"])
def api_update_consultant(consultant_id):
    if not get_consultant(consultant_id):
        return jsonify({"ok": False, "message": "Consultor não encontrado"}), 404
    data = request.json or {}
    allowed = ("name", "context", "slot_duration_minutes", "weekly_availability", "reminder_hours_before", "status", "self_availability_enabled")
    fields = {k: v for k, v in data.items() if k in allowed}
    if "status" in fields and fields["status"] not in ("active", "inactive"):
        return jsonify({"ok": False, "message": "status só pode ser alternado entre active/inactive por aqui (confirmação inicial é feita pelo próprio consultor no WhatsApp)"}), 400
    if not fields:
        return jsonify({"ok": False, "message": "Nada para atualizar"}), 400
    update_consultant(consultant_id, fields)
    return jsonify({"ok": True})


@app.route("/api/whatsapp/consultants/<int:consultant_id>", methods=["DELETE"])
def api_delete_consultant(consultant_id):
    if not get_consultant(consultant_id):
        return jsonify({"ok": False, "message": "Consultor não encontrado"}), 404
    delete_consultant(consultant_id)
    return jsonify({"ok": True})


@app.route("/api/whatsapp/consultants/<int:consultant_id>/resend-portal-link", methods=["POST"])
def api_resend_portal_link(consultant_id):
    """Regenera o token (invalida o link antigo) e reenvia por WhatsApp —
    cobre link perdido/vazado, sem precisar re-confirmar o cadastro todo."""
    consultant = get_consultant(consultant_id)
    if not consultant:
        return jsonify({"ok": False, "message": "Consultor não encontrado"}), 404
    if consultant["status"] != "active":
        return jsonify({"ok": False, "message": "Só é possível reenviar o link de um consultor ativo"}), 400
    token = regenerate_portal_token(consultant_id)
    try:
        evolution.send_text(consultant["wa_session_name"], _phone_from_wa_id(consultant["wa_id"]),
                             f"Aqui está o link atualizado da sua agenda: {portal_link(token)}")
    except EvolutionError as e:
        return jsonify({"ok": False, "message": f"Token atualizado, mas não consegui mandar por WhatsApp: {e}"}), 502
    return jsonify({"ok": True})


@app.route("/api/whatsapp/consultants/<int:consultant_id>/resend-invite", methods=["POST"])
def api_resend_consultant_invite(consultant_id):
    """Reenvia o convite inicial de confirmação (botões Sim/Não) — cobre o caso
    da primeira mensagem não ter chegado. Só faz sentido com confirmação pendente;
    depois de confirmado/recusado, quem reenvia é o fluxo de portal (resend-portal-link)."""
    consultant = get_consultant(consultant_id)
    if not consultant:
        return jsonify({"ok": False, "message": "Consultor não encontrado"}), 404
    if consultant["status"] != "pending_confirmation":
        return jsonify({"ok": False, "message": "Só é possível reenviar convite para consultor aguardando confirmação"}), 400
    try:
        _send_consultant_invite_message(consultant)
    except EvolutionError as e:
        return jsonify({"ok": False, "message": f"Não consegui reenviar por WhatsApp: {e}"}), 502
    return jsonify({"ok": True})


@app.route("/api/whatsapp/accounts/<int:account_id>/nomenclature", methods=["GET"])
def api_get_nomenclature(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    return jsonify({"nomenclature": get_nomenclature(account.get("user_id"))})


@app.route("/api/whatsapp/accounts/<int:account_id>/nomenclature", methods=["PATCH"])
def api_set_nomenclature(account_id):
    # Nomenclatura é por CLIENTE, não por conta — grava em whatsapp_client_settings
    # chaveada no user_id da conta, então vale pra todas as outras contas do
    # mesmo cliente também (mesmo comportamento pro lado admin e client-portal).
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    if not account.get("user_id"):
        return jsonify({"ok": False, "message": "Essa conta não está vinculada a um cliente"}), 400
    data = request.json or {}
    return jsonify({"ok": True, "nomenclature": set_nomenclature(account["user_id"], data)})


@app.route("/api/whatsapp/accounts/<int:account_id>/appointments", methods=["GET"])
def api_list_appointments(account_id):
    if not get_account(account_id):
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    date_from = request.args.get("from") or None
    date_to = request.args.get("to") or None
    include_past = request.args.get("include_past") == "1"
    return jsonify({"appointments": get_appointments(
        account_id, upcoming_only=not include_past, date_from=date_from, date_to=date_to,
    )})


@app.route("/api/whatsapp/appointments/<int:appointment_id>/cancel", methods=["POST"])
def api_cancel_appointment(appointment_id):
    cancel_appointment(appointment_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Área do cliente — autenticada por X-Oraculo-Key (mesma chave do
# ai_oraculo_saas, resolvida por resolve_client_from_request()). Cada rota
# confere que o account_id/chat_id/consultant_id/appointment_id da URL
# pertence ao user_id resolvido ANTES de delegar pra mesma view admin que já
# existe — nunca aceita esses ids do cliente sem checar a posse primeiro.
# ---------------------------------------------------------------------------

def _require_client():
    user_id = resolve_client_from_request()
    if not user_id:
        return None, (jsonify({"ok": False, "message": "Chave de acesso inválida"}), 401)
    return user_id, None


def _require_crm_medico(user_id):
    """Gate das rotas exclusivas do CRM médico (checklist/painel da
    secretária) — devolve uma resposta de erro se o plano do cliente não
    estiver no modo 'crm_medico', ou None se puder seguir."""
    if plan_booking_mode(user_id) != "crm_medico":
        return jsonify({"ok": False, "message": "O CRM médico não está ativado no plano desta conta."}), 403
    return None


def _not_found(msg="Não encontrado"):
    return jsonify({"ok": False, "message": msg}), 404


def _account_owner(account_id):
    account = get_account(account_id)
    return account.get("user_id") if account else None


def _chat_owner(chat_id):
    chat = get_chat(chat_id)
    return _account_owner(chat["account_id"]) if chat else None


def _consultant_owner(consultant_id):
    consultant = get_consultant(consultant_id)
    return _account_owner(consultant["account_id"]) if consultant else None


def _appointment_owner(appointment_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT c.account_id FROM whatsapp_appointments a
               JOIN whatsapp_consultants c ON c.id = a.consultant_id
               WHERE a.id = %s""",
            (appointment_id,),
        )
        row = cur.fetchone()
        return _account_owner(row[0]) if row else None
    finally:
        conn.close()


def _checklist_item_owner(item_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT con.account_id FROM whatsapp_checklist_items i
               JOIN whatsapp_appointments a ON a.id = i.appointment_id
               JOIN whatsapp_consultants con ON con.id = a.consultant_id
               WHERE i.id = %s""",
            (item_id,),
        )
        row = cur.fetchone()
        return _account_owner(row[0]) if row else None
    finally:
        conn.close()


def _contact_owner(contact_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT account_id FROM whatsapp_contacts WHERE id = %s", (contact_id,))
        row = cur.fetchone()
        return _account_owner(row[0]) if row else None
    finally:
        conn.close()


def _patient_document_owner(doc_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT account_id FROM whatsapp_patient_documents WHERE id = %s", (doc_id,))
        row = cur.fetchone()
        return _account_owner(row[0]) if row else None
    finally:
        conn.close()


def _send_patient_document_file(doc_id):
    doc = get_patient_document(doc_id)
    if not doc or doc["status"] != "stored" or not doc.get("storage_path"):
        return _not_found("Documento não disponível")
    base = os.path.realpath(MEDIA_STORAGE_DIR)
    full = os.path.realpath(os.path.join(base, doc["storage_path"]))
    if not (full == base or full.startswith(base + os.sep)) or not os.path.isfile(full):
        return _not_found("Arquivo não encontrado")
    return send_file(
        full,
        mimetype=doc["mime_type"] or "application/octet-stream",
        as_attachment=False,
        download_name=doc["original_name"] or f"documento-{doc_id}",
    )


# ---- Contas: conectar/desconectar (criar/vincular conexão continua sendo só admin) ----

@app.route("/api/client-portal/accounts", methods=["GET"])
def cp_list_accounts():
    user_id, err = _require_client()
    if err: return err
    return jsonify({"accounts": get_accounts(user_id=user_id)})


@app.route("/api/client-portal/booking-mode", methods=["GET"])
def cp_booking_mode():
    """Usado pelos frontends (area-cliente.html, painel-secretaria.html) pra
    saber se o cliente está no modo Consultores, CRM médico, ou nenhum, e
    mostrar/esconder telas de acordo — sem precisar duplicar essa lógica."""
    user_id, err = _require_client()
    if err: return err
    return jsonify({"booking_mode": plan_booking_mode(user_id)})


@app.route("/api/client-portal/accounts/<int:account_id>/weekly-summary-schedule", methods=["PUT"])
def cp_set_weekly_summary_schedule(account_id):
    """Dia/hora em que os médicos dessa clínica recebem o resumo semanal da
    agenda (mesmo horário pra todos os médicos da conta) — lido por
    _due_weekly_consultants() no loop de fundo."""
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    data = request.json or {}
    weekday = data.get("weekday")
    hour = data.get("hour")
    if not isinstance(weekday, int) or not (0 <= weekday <= 6):
        return jsonify({"ok": False, "message": "Dia da semana inválido"}), 400
    if not isinstance(hour, int) or not (0 <= hour <= 23):
        return jsonify({"ok": False, "message": "Hora inválida"}), 400
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE whatsapp_accounts SET weekly_summary_weekday = %s, weekly_summary_hour = %s WHERE id = %s",
            (weekday, hour, account_id),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/client-portal/accounts/<int:account_id>/resend-weekly-summary", methods=["POST"])
def cp_resend_weekly_summary(account_id):
    """Reenvio manual, sob demanda — a secretária digita o próprio WhatsApp e
    recebe na hora um resumo dos agendamentos confirmados dos próximos 7 dias
    de TODOS os médicos da clínica (diferente do resumo automático semanal,
    que é por médico individual, um de cada vez)."""
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    err = _require_crm_medico(user_id)
    if err: return err
    account = get_account(account_id)
    if not account:
        return _not_found("Conta não encontrada")
    data = request.json or {}
    phone = re.sub(r"\D", "", data.get("phone") or "")
    if not phone:
        return jsonify({"ok": False, "message": "Informe o número de WhatsApp"}), 400

    appts = get_appointments(account_id)
    limit = (datetime.datetime.now(booking_flow.LOCAL_TZ) + datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
    week_appts = [a for a in appts if a["status"] == "confirmed" and a["scheduled_at"] <= limit]
    if week_appts:
        linhas = "\n".join(
            f"- {a['scheduled_at'][8:10]}/{a['scheduled_at'][5:7]} {a['scheduled_at'][11:16]} · "
            f"{a['consultant_name']} · {a['client_name'] or a['client_wa_id']}"
            for a in week_appts[:40]
        )
        texto = f"Resumo da semana — {account['label']}:\n{linhas}"
    else:
        texto = f"Resumo da semana — {account['label']}: nenhuma consulta confirmada nos próximos 7 dias."
    try:
        evolution.send_text(account["wa_session_name"], phone, texto)
    except EvolutionError as e:
        log_event(account_id, "weekly_summary_resend_failed", level="error", detail={"error": str(e)})
        return jsonify({"ok": False, "message": "Erro ao enviar pelo WhatsApp. Confirme o número e tente de novo."}), 502
    return jsonify({"ok": True})


@app.route("/api/client-portal/settings/nomenclature", methods=["GET"])
def cp_get_nomenclature():
    user_id, err = _require_client()
    if err: return err
    return jsonify({"nomenclature": get_nomenclature(user_id)})


@app.route("/api/client-portal/settings/nomenclature", methods=["PATCH"])
def cp_set_nomenclature():
    # Vale pra todas as contas de WhatsApp do cliente logado, não só pra uma —
    # é por isso que essa rota não leva account_id na URL.
    user_id, err = _require_client()
    if err: return err
    data = request.json or {}
    return jsonify({"ok": True, "nomenclature": set_nomenclature(user_id, data)})


@app.route("/api/client-portal/accounts/<int:account_id>/connect", methods=["POST"])
def cp_connect_account(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    return api_connect_account(account_id)


@app.route("/api/client-portal/accounts/<int:account_id>/status", methods=["GET"])
def cp_account_status(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    return api_account_status(account_id)


@app.route("/api/client-portal/accounts/<int:account_id>/disconnect", methods=["POST"])
def cp_disconnect_account(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    return api_disconnect_account(account_id)


# ---- Conversas: acesso completo, escopado às próprias contas ----

@app.route("/api/client-portal/chats", methods=["GET"])
def cp_list_chats():
    user_id, err = _require_client()
    if err: return err
    account_id = request.args.get("account_id", type=int)
    if not account_id or _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    return api_list_chats(account_id)


@app.route("/api/client-portal/accounts/<int:account_id>/chats/start", methods=["POST"])
def cp_start_chat(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    return api_start_chat(account_id)


@app.route("/api/client-portal/chats/<int:chat_id>", methods=["PATCH"])
def cp_update_chat(chat_id):
    user_id, err = _require_client()
    if err: return err
    if _chat_owner(chat_id) != user_id:
        return _not_found("Conversa não encontrada")
    return api_update_chat(chat_id)


@app.route("/api/client-portal/chats/<int:chat_id>/messages", methods=["GET"])
def cp_list_messages(chat_id):
    user_id, err = _require_client()
    if err: return err
    if _chat_owner(chat_id) != user_id:
        return _not_found("Conversa não encontrada")
    return api_list_messages(chat_id)


@app.route("/api/client-portal/chats/<int:chat_id>/messages", methods=["POST"])
def cp_send_message(chat_id):
    user_id, err = _require_client()
    if err: return err
    if _chat_owner(chat_id) != user_id:
        return _not_found("Conversa não encontrada")
    return api_send_message(chat_id)


@app.route("/api/client-portal/chats/<int:chat_id>/read", methods=["POST"])
def cp_mark_chat_read(chat_id):
    user_id, err = _require_client()
    if err: return err
    if _chat_owner(chat_id) != user_id:
        return _not_found("Conversa não encontrada")
    return api_mark_chat_read(chat_id)


# ---- Agenda: controle total, escopado às próprias contas ----

@app.route("/api/client-portal/accounts/<int:account_id>/consultants", methods=["GET"])
def cp_list_consultants(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    return api_list_consultants(account_id)


@app.route("/api/client-portal/accounts/<int:account_id>/consultants", methods=["POST"])
def cp_create_consultant(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    return api_create_consultant(account_id)


@app.route("/api/client-portal/accounts/<int:account_id>/contacts", methods=["GET"])
def cp_list_contacts(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    return jsonify({"contacts": get_account_contacts(account_id)})


@app.route("/api/client-portal/consultants/<int:consultant_id>/free-slots", methods=["GET"])
def cp_free_slots(consultant_id):
    user_id, err = _require_client()
    if err: return err
    if _consultant_owner(consultant_id) != user_id:
        return _not_found("Consultor não encontrado")
    consultant = get_consultant(consultant_id)
    slots = booking_flow.compute_free_slots(consultant)
    return jsonify({"slots": [s.isoformat() for s in slots]})


@app.route("/api/client-portal/consultants/<int:consultant_id>/appointments", methods=["POST"])
def cp_create_appointment(consultant_id):
    """Só o modo CRM médico usa isso — é a única forma de marcar uma consulta
    nova quando o self-service do paciente via WhatsApp está desligado (ver
    plan_booking_mode/booking_flow.py:handle_incoming)."""
    user_id, err = _require_client()
    if err: return err
    if _consultant_owner(consultant_id) != user_id:
        return _not_found("Consultor não encontrado")
    err = _require_crm_medico(user_id)
    if err: return err
    consultant = get_consultant(consultant_id)
    return _create_appointment_for_consultant(consultant, request.json or {})


@app.route("/api/client-portal/consultants/<int:consultant_id>", methods=["PATCH"])
def cp_update_consultant(consultant_id):
    user_id, err = _require_client()
    if err: return err
    if _consultant_owner(consultant_id) != user_id:
        return _not_found("Consultor não encontrado")
    return api_update_consultant(consultant_id)


@app.route("/api/client-portal/consultants/<int:consultant_id>", methods=["DELETE"])
def cp_delete_consultant(consultant_id):
    user_id, err = _require_client()
    if err: return err
    if _consultant_owner(consultant_id) != user_id:
        return _not_found("Consultor não encontrado")
    return api_delete_consultant(consultant_id)


@app.route("/api/client-portal/consultants/<int:consultant_id>/resend-portal-link", methods=["POST"])
def cp_resend_portal_link(consultant_id):
    user_id, err = _require_client()
    if err: return err
    if _consultant_owner(consultant_id) != user_id:
        return _not_found("Consultor não encontrado")
    return api_resend_portal_link(consultant_id)


@app.route("/api/client-portal/consultants/<int:consultant_id>/resend-invite", methods=["POST"])
def cp_resend_consultant_invite(consultant_id):
    user_id, err = _require_client()
    if err: return err
    if _consultant_owner(consultant_id) != user_id:
        return _not_found("Consultor não encontrado")
    return api_resend_consultant_invite(consultant_id)


@app.route("/api/client-portal/accounts/<int:account_id>/appointments", methods=["GET"])
def cp_list_appointments(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    return api_list_appointments(account_id)


@app.route("/api/client-portal/appointments/<int:appointment_id>/cancel", methods=["POST"])
def cp_cancel_appointment(appointment_id):
    user_id, err = _require_client()
    if err: return err
    if _appointment_owner(appointment_id) != user_id:
        return _not_found("Agendamento não encontrado")
    return api_cancel_appointment(appointment_id)


# ---- CRM médico / painel da secretária: conclusão de consulta e checklist
# de acompanhamento do paciente, configurável por conta (clínica) ----

@app.route("/api/client-portal/appointments/<int:appointment_id>/complete", methods=["POST"])
def cp_complete_appointment(appointment_id):
    user_id, err = _require_client()
    if err: return err
    if _appointment_owner(appointment_id) != user_id:
        return _not_found("Agendamento não encontrado")
    err = _require_crm_medico(user_id)
    if err: return err
    if not mark_appointment_completed(appointment_id):
        return jsonify({"ok": False, "message": "Agendamento não encontrado ou já concluído/cancelado"}), 400
    return jsonify({"ok": True})


@app.route("/api/client-portal/accounts/<int:account_id>/checklist-template", methods=["GET"])
def cp_get_checklist_template(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    err = _require_crm_medico(user_id)
    if err: return err
    return jsonify({"steps": get_checklist_template(account_id)})


@app.route("/api/client-portal/accounts/<int:account_id>/checklist-template", methods=["PUT"])
def cp_set_checklist_template(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    err = _require_crm_medico(user_id)
    if err: return err
    data = request.json or {}
    steps = data.get("steps") or []
    account = get_account(account_id)
    # Sem essa checagem, set_checklist_template() derruba os 3 flags de
    # destinatário pra False em silêncio quando o texto vem vazio (ver
    # server.py mais abaixo) — e quem tá preenchendo o formulário só descobre
    # que "não salvou" ao reabrir a tela, sem nenhuma explicação do porquê.
    for step in steps:
        if not isinstance(step, dict):
            continue
        label = (step.get("label") or "").strip()
        wants_message = step.get("notify_patient") or step.get("notify_consultant") or step.get("notify_secretary")
        if label and wants_message and not (step.get("auto_message_template") or "").strip():
            return jsonify({
                "ok": False,
                "message": f'A etapa "{label}" está marcada pra enviar mensagem automática, mas o texto da '
                           f'mensagem está vazio. Preencha o texto ou desmarque a opção.',
            }), 400
        # Marcar "avisar a secretária" sem telefone de secretária cadastrado
        # nunca manda nada (mark_checklist_item não tem pra quem mandar) —
        # bloqueado aqui, na configuração, em vez de só se descobrir depois
        # que um paciente já devia ter passado por essa etapa.
        if label and step.get("notify_secretary") and not account.get("secretary_contact_id"):
            return jsonify({
                "ok": False,
                "message": f'A etapa "{label}" está marcada pra notificar a secretária, mas nenhum telefone de '
                           f'secretária foi cadastrado para esta clínica. Cadastre o telefone da secretária primeiro.',
            }), 400
    return jsonify({"ok": True, "steps": set_checklist_template(account_id, steps)})


@app.route("/api/client-portal/accounts/<int:account_id>/secretary-contact", methods=["PATCH"])
def cp_set_secretary_contact(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    err = _require_crm_medico(user_id)
    if err: return err
    phone = re.sub(r"\D", "", (request.json or {}).get("phone") or "")
    set_account_secretary_contact(account_id, phone)
    account = get_account(account_id)
    return jsonify({
        "ok": True,
        "secretary_contact_id": account.get("secretary_contact_id"),
        "secretary_wa_id": account.get("secretary_wa_id"),
        "secretary_push_name": account.get("secretary_push_name"),
    })


@app.route("/api/client-portal/appointments/<int:appointment_id>/checklist", methods=["GET"])
def cp_get_appointment_checklist(appointment_id):
    user_id, err = _require_client()
    if err: return err
    if _appointment_owner(appointment_id) != user_id:
        return _not_found("Agendamento não encontrado")
    err = _require_crm_medico(user_id)
    if err: return err
    return jsonify({"items": get_checklist_items_for_appointment(appointment_id)})


@app.route("/api/client-portal/accounts/<int:account_id>/checklist-items", methods=["GET"])
def cp_list_checklist_items(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    err = _require_crm_medico(user_id)
    if err: return err
    status = request.args.get("status") or None
    return jsonify({"items": get_checklist_items_for_account(account_id, status)})


@app.route("/api/client-portal/checklist-items/<int:item_id>", methods=["PATCH"])
def cp_update_checklist_item(item_id):
    user_id, err = _require_client()
    if err: return err
    if _checklist_item_owner(item_id) != user_id:
        return _not_found("Item não encontrado")
    err = _require_crm_medico(user_id)
    if err: return err
    new_status = (request.json or {}).get("status")
    if new_status not in ("pending", "done", "skipped"):
        return jsonify({"ok": False, "message": "Status inválido"}), 400
    result = mark_checklist_item(item_id, new_status)
    if not result:
        return _not_found("Item não encontrado")
    if new_status == "done":
        local_scheduled_at = result["scheduled_at"].astimezone(booking_flow.LOCAL_TZ)
        ctx = {
            "paciente": result["client_push_name"] or _phone_from_wa_id(result["client_wa_id"]),
            "medico": result["consultant_name"],
            "data": local_scheduled_at.strftime("%d/%m/%Y"),
            "hora": local_scheduled_at.strftime("%H:%M"),
            "assunto": result["subject"] or "",
            "clinica": result["account_label"],
        }
        texto = _render_checklist_message(result["auto_message_template"], ctx)
        # Um texto só, renderizado 1x — os 3 checkboxes da etapa só decidem
        # quem recebe essa mesma mensagem. Cada destinatário tem sua própria
        # marca de "já enviado" e falha independente dos outros (ex: número
        # do médico inválido não deve impedir o envio pro paciente).
        candidates = [
            ("patient", result["notify_patient"], result["client_wa_id"], result["auto_message_sent_patient_at"]),
            ("consultant", result["notify_consultant"], result["consultant_wa_id"], result["auto_message_sent_consultant_at"]),
            ("secretary", result["notify_secretary"], result["secretary_wa_id"], result["auto_message_sent_secretary_at"]),
        ]
        for recipient, wants, wa_id, already_sent in candidates:
            if not wants or already_sent:
                continue
            if not wa_id:
                # Só deveria acontecer pra secretary (patient/consultant
                # sempre têm wa_id) — cp_set_checklist_template já bloqueia
                # notify_secretary sem telefone cadastrado; isso aqui é
                # defesa em profundidade, não trava a conclusão do item.
                log_event(None, "checklist_auto_message_skipped_no_contact", level="warning",
                          detail={"item_id": item_id, "recipient": recipient})
                continue
            try:
                evolution.send_text(result["wa_session_name"], _phone_from_wa_id(wa_id), texto)
                _mark_checklist_auto_message_sent(item_id, recipient)
            except EvolutionError as e:
                log_event(None, "checklist_auto_message_failed", level="error",
                          detail={"item_id": item_id, "recipient": recipient, "error": str(e)})
    return jsonify({"ok": True})


@app.route("/api/client-portal/accounts/<int:account_id>/patients-with-documents", methods=["GET"])
def cp_patients_with_documents(account_id):
    user_id, err = _require_client()
    if err: return err
    if _account_owner(account_id) != user_id:
        return _not_found("Conta não encontrada")
    err = _require_crm_medico(user_id)
    if err: return err
    return jsonify({"patients": get_patients_with_documents(account_id)})


@app.route("/api/client-portal/contacts/<int:contact_id>/documents", methods=["GET"])
def cp_list_patient_documents(contact_id):
    user_id, err = _require_client()
    if err: return err
    if _contact_owner(contact_id) != user_id:
        return _not_found("Paciente não encontrado")
    err = _require_crm_medico(user_id)
    if err: return err
    return jsonify({"documents": get_patient_documents_for_contact(contact_id)})


@app.route("/api/client-portal/patient-documents/<int:doc_id>/file", methods=["GET"])
def cp_get_patient_document_file(doc_id):
    user_id, err = _require_client()
    if err: return err
    if _patient_document_owner(doc_id) != user_id:
        return _not_found("Documento não encontrado")
    err = _require_crm_medico(user_id)
    if err: return err
    return _send_patient_document_file(doc_id)


@app.route("/api/client-portal/patient-documents/<int:doc_id>", methods=["DELETE"])
def cp_hide_patient_document(doc_id):
    user_id, err = _require_client()
    if err: return err
    if _patient_document_owner(doc_id) != user_id:
        return _not_found("Documento não encontrado")
    err = _require_crm_medico(user_id)
    if err: return err
    if not hide_patient_document(doc_id):
        return _not_found("Documento não encontrado")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Portal do consultor — autenticado por token (link mandado por WhatsApp),
# nunca por sessão de admin. Cada rota resolve o consultor pelo token e só
# enxerga/mexe nos dados DELE — nunca aceita account_id/consultant_id vindo
# do cliente sem checar contra o token.
# ---------------------------------------------------------------------------

@app.route("/api/consultant-portal/<token>/me", methods=["GET"])
def api_portal_me(token):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    return jsonify({
        "name": consultant["name"],
        "account_label": consultant["account_label"],
        "slot_duration_minutes": consultant["slot_duration_minutes"],
        "weekly_availability": consultant["weekly_availability"],
        "self_availability_enabled": consultant["self_availability_enabled"],
    })


@app.route("/api/consultant-portal/<token>/appointments", methods=["GET"])
def api_portal_appointments(token):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    pending, upcoming, history = get_consultant_appointments(consultant["id"])
    return jsonify({"pending": pending, "upcoming": upcoming, "history": history})


def get_account_contacts(account_id):
    """Contatos já conhecidos na conexão de WhatsApp da conta — usado pelo
    formulário de novo agendamento, pra escolher em vez de só digitar o
    telefone. Reaproveitado tanto pelo portal do consultor (por token) quanto
    pelo client-portal (por API key, painel da secretária)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT wa_id, COALESCE(name, push_name) FROM whatsapp_contacts
               WHERE account_id = %s AND status = 'active' AND wa_id LIKE '%%@s.whatsapp.net'
               ORDER BY COALESCE(name, push_name) NULLS LAST""",
            (account_id,),
        )
        return [{"phone": r[0].split("@")[0], "name": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def _create_appointment_for_consultant(consultant, data):
    """Corpo comum de criação de agendamento — usado tanto pelo próprio
    consultor (portal por token) quanto pela secretária (client-portal, modo
    CRM médico). Sempre nasce 'confirmed' direto (notify_consultant=False):
    quem cria já é quem administra a agenda, não precisa de auto-confirmação."""
    phone = re.sub(r"\D", "", data.get("phone") or "")
    name = (data.get("name") or "").strip()
    subject = (data.get("subject") or "").strip() or None
    scheduled_at_raw = data.get("scheduled_at")
    if not phone or not scheduled_at_raw:
        return jsonify({"ok": False, "message": "Telefone e horário são obrigatórios"}), 400
    try:
        scheduled_at = datetime.datetime.fromisoformat(scheduled_at_raw)
    except ValueError:
        return jsonify({"ok": False, "message": "Horário inválido"}), 400

    wa_id = f"{phone}@s.whatsapp.net"
    client_contact_id = get_or_create_contact(consultant["account_id"], wa_id, name or None)
    ok = booking_flow.book_appointment(consultant, client_contact_id, wa_id, name, scheduled_at, notify_consultant=False, subject=subject)
    if not ok:
        return jsonify({"ok": False, "message": "Esse horário não está mais livre"}), 409
    return jsonify({"ok": True}), 201


@app.route("/api/consultant-portal/<token>/contacts", methods=["GET"])
def api_portal_contacts(token):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    return jsonify({"contacts": get_account_contacts(consultant["account_id"])})


@app.route("/api/consultant-portal/<token>/free-slots", methods=["GET"])
def api_portal_free_slots(token):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    slots = booking_flow.compute_free_slots(consultant)
    return jsonify({"slots": [s.isoformat() for s in slots]})


def _consultant_sees_contact(consultant_id, contact_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM whatsapp_appointments WHERE consultant_id = %s AND client_contact_id = %s LIMIT 1",
            (consultant_id, contact_id),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def _consultant_sees_document(consultant_id, doc_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT 1 FROM whatsapp_patient_documents d
               JOIN whatsapp_appointments a ON a.client_contact_id = d.contact_id
               WHERE d.id = %s AND a.consultant_id = %s LIMIT 1""",
            (doc_id, consultant_id),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


@app.route("/api/consultant-portal/<token>/patients-with-documents", methods=["GET"])
def api_portal_patients_with_documents(token):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    return jsonify({"patients": get_patients_with_documents_for_consultant(consultant["id"])})


@app.route("/api/consultant-portal/<token>/contacts/<int:contact_id>/documents", methods=["GET"])
def api_portal_patient_documents(token, contact_id):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    if not _consultant_sees_contact(consultant["id"], contact_id):
        return jsonify({"ok": False, "message": "Paciente não encontrado"}), 404
    return jsonify({"documents": get_patient_documents_for_contact(contact_id)})


@app.route("/api/consultant-portal/<token>/patient-documents/<int:doc_id>/file", methods=["GET"])
def api_portal_patient_document_file(token, doc_id):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    if not _consultant_sees_document(consultant["id"], doc_id):
        return jsonify({"ok": False, "message": "Documento não encontrado"}), 404
    return _send_patient_document_file(doc_id)


@app.route("/api/consultant-portal/<token>/appointments", methods=["POST"])
def api_portal_create_appointment(token):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    return _create_appointment_for_consultant(consultant, request.json or {})


@app.route("/api/consultant-portal/<token>/appointments/<int:appointment_id>/cancel", methods=["POST"])
def api_portal_cancel_appointment(token, appointment_id):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    result = booking_flow.cancel_appointment_and_notify(appointment_id, consultant["id"])
    if result == "not_found":
        return jsonify({"ok": False, "message": "Agendamento não encontrado"}), 404
    if result == "forbidden":
        return jsonify({"ok": False, "message": "Esse agendamento não é seu"}), 403
    return jsonify({"ok": True})


@app.route("/api/consultant-portal/<token>/appointments/<int:appointment_id>/confirm", methods=["POST"])
def api_portal_confirm_appointment(token, appointment_id):
    """Confirma um pedido que veio do self-service do cliente pelo WhatsApp
    (status 'pending_consultant') — avisa o cliente que foi confirmado."""
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    result = booking_flow.confirm_appointment_and_notify(appointment_id, consultant["id"])
    if result == "not_found":
        return jsonify({"ok": False, "message": "Agendamento não encontrado ou já resolvido"}), 404
    if result == "forbidden":
        return jsonify({"ok": False, "message": "Esse agendamento não é seu"}), 403
    return jsonify({"ok": True})


@app.route("/api/consultant-portal/<token>/appointments/<int:appointment_id>/decline", methods=["POST"])
def api_portal_decline_appointment(token, appointment_id):
    """Recusa um pedido que veio do self-service do cliente pelo WhatsApp
    (status 'pending_consultant') — libera o horário e avisa o cliente."""
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    result = booking_flow.decline_appointment_and_notify(appointment_id, consultant["id"])
    if result == "not_found":
        return jsonify({"ok": False, "message": "Agendamento não encontrado ou já resolvido"}), 404
    if result == "forbidden":
        return jsonify({"ok": False, "message": "Esse agendamento não é seu"}), 403
    return jsonify({"ok": True})


@app.route("/api/consultant-portal/<token>/appointments/<int:appointment_id>/reschedule", methods=["POST"])
def api_portal_reschedule_appointment(token, appointment_id):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    data = request.json or {}
    try:
        new_scheduled_at = datetime.datetime.fromisoformat(data.get("new_scheduled_at") or "")
    except ValueError:
        return jsonify({"ok": False, "message": "Horário inválido"}), 400
    result = booking_flow.reschedule_appointment_and_notify(appointment_id, consultant["id"], new_scheduled_at)
    if result == "not_found":
        return jsonify({"ok": False, "message": "Agendamento não encontrado"}), 404
    if result == "forbidden":
        return jsonify({"ok": False, "message": "Esse agendamento não é seu"}), 403
    if result == "conflict":
        return jsonify({"ok": False, "message": "Esse horário não está mais livre"}), 409
    return jsonify({"ok": True})


@app.route("/api/consultant-portal/<token>/availability", methods=["PATCH"])
def api_portal_update_availability(token):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    data = request.json or {}
    if "weekly_availability" not in data:
        return jsonify({"ok": False, "message": "Nada para atualizar"}), 400
    update_consultant(consultant["id"], {"weekly_availability": data["weekly_availability"]})
    return jsonify({"ok": True})


def _client_api_key(user_id):
    # Leitura direta em users (mesmo ai_tutor_db, mesma conexão — mesmo padrão
    # já usado em get_clients()); nunca escrevemos nessa tabela daqui.
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT api_key FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _handle_ai_auto_reply(account, chat_id, wa_id, incoming_text):
    """Resposta automática via IA: só roda se a CONVERSA tiver auto-reply
    ligado (whatsapp_chats.ai_auto_reply_enabled — toggle por conversa, não
    mais por conta) E a conta estiver vinculada a uma área (o vínculo é feito
    no cadastro do cliente, no Oráculo). whatsapp_accounts.ai_auto_reply_enabled
    só define o valor com que toda conversa NOVA começa (get_or_create_chat);
    a partir daí quem manda é o toggle da conversa. Chama /api/chat de lá com
    a api_key do próprio cliente — nunca chama com privilégio nenhum além do
    que o cliente já tem. Roda numa thread separada (disparada pelo webhook)
    pra não segurar a resposta HTTP do webhook enquanto o LLM processa."""
    if not incoming_text:
        return
    chat = get_chat(chat_id)
    if not chat or not chat.get("ai_auto_reply_enabled") or not account.get("area_id"):
        return

    api_key = _client_api_key(account["user_id"]) if account.get("user_id") else None
    if not api_key:
        log_event(account["id"], "ai_auto_reply_skipped", level="warn",
                   detail={"reason": "conta sem cliente vinculado (ou cliente sem api_key)"})
        return

    # Últimas mensagens da conversa (a mais recente é a própria incoming_text,
    # já salva por save_message antes desta função ser chamada — descartada
    # aqui porque já vai separada no campo "message"). O corte pro orçamento
    # de tokens do plano do cliente acontece do lado do ai_oraculo_saas, que
    # é quem conhece o plano; aqui só manda um teto generoso de mensagens brutas.
    history = [
        {"role": "user" if m["direction"] == "in" else "assistant", "content": m["body"]}
        for m in list_messages(chat_id, limit=21)[:-1]
        if m.get("message_type") == "text" and m.get("body")
    ]

    base_url = (ORACULO_API_CONFIG.get("base_url") or "http://127.0.0.1:5001").rstrip("/")
    try:
        resp = requests.post(
            f"{base_url}/api/chat",
            headers={"X-Oraculo-Key": api_key},
            json={"message": incoming_text, "area_ids": [account["area_id"]], "source": "whatsapp", "history": history},
            timeout=25,
        )
        resp.raise_for_status()
        reply_text = (resp.json() or {}).get("response")
        if not reply_text:
            return
        result = evolution.send_text(account["wa_session_name"], _phone_from_wa_id(wa_id), reply_text)
        wa_message_id = ((result or {}).get("key") or {}).get("id")
        save_message(chat_id, account["id"], "out", reply_text, wa_message_id=wa_message_id)
    except Exception as e:
        log_event(account["id"], "ai_auto_reply_failed", level="error", detail={"error": str(e)})


def _handle_unrelated_received_usage(account):
    """Reporta pro Oráculo 1 mensagem recebida numa conexão SEM área vinculada
    — "não relacionada às áreas selecionadas" no cadastro do cliente. O
    Oráculo decide se isso é contado só ou também cobrado (plans.
    charge_unrelated_received_messages). Roda em thread separada, erro só
    loga — nunca derruba o webhook."""
    api_key = _client_api_key(account["user_id"]) if account.get("user_id") else None
    if not api_key:
        return  # conta sem cliente vinculado — nada pra medir/cobrar
    base_url = (ORACULO_API_CONFIG.get("base_url") or "http://127.0.0.1:5001").rstrip("/")
    try:
        requests.post(f"{base_url}/api/whatsapp/received-usage", headers={"X-Oraculo-Key": api_key}, timeout=10)
    except Exception as e:
        log_event(account["id"], "received_usage_report_failed", level="error", detail={"error": str(e)})


@app.route("/webhooks/evolution", methods=["POST"])
def webhook_evolution():
    payload = request.json or {}
    event = (payload.get("event") or "").lower().replace("_", ".")

    # Log cru sempre, antes de tentar interpretar — garante que temos o
    # payload real da Evolution API pra corrigir o parser abaixo se o
    # formato não bater exatamente com o esperado (mesma disciplina usada
    # com o conector antigo, ver plano).
    log_event(None, "webhook_received", detail=payload)

    instance_name = payload.get("instance")
    account = get_account_by_session(instance_name) if instance_name else None
    if not account:
        return jsonify({"ok": True})  # instância desconhecida (ex: conta de teste já removida) — ignora

    data = payload.get("data") or {}

    if event == "connection.update":
        evo_state = data.get("state")
        our_status = EVOLUTION_STATUS_MAP.get(evo_state)
        if our_status:
            update_account_status(account["id"], our_status, connected=(our_status == "connected"))
            log_event(account["id"], "status_via_webhook", detail={"evolution_state": evo_state})

    elif event == "messages.upsert":
        key = data.get("key") or {}
        message = data.get("message") or {}
        # Resposta de lista/botão (fluxo de agendamento e confirmação de
        # consultor) — shape confirmado lendo evolution-api/src/utils/
        # getConversationMessage.ts direto no servidor, não documentação.
        selected_id = (
            (message.get("listResponseMessage") or {}).get("singleSelectReply", {}).get("selectedRowId")
            or (message.get("templateButtonReplyMessage") or {}).get("selectedId")
            or (message.get("buttonsResponseMessage") or {}).get("selectedButtonId")
        )
        if key.get("fromMe") and not selected_id:
            # eco de texto solto que a gente mesmo mandou (já gravado no envio) —
            # descarta. Resposta de lista/botão passa mesmo com fromMe=true: o bot
            # nunca gera esse tipo de mensagem sozinho, só existe quando um humano
            # toca uma opção — inclusive em chat consigo mesmo (conta = consultor),
            # onde toda mensagem vem fromMe=true por natureza do self-chat do WhatsApp.
            return jsonify({"ok": True})
        wa_id = key.get("remoteJid")
        list_title = (message.get("listResponseMessage") or {}).get("title")
        body = list_title or message.get("conversation") or (message.get("extendedTextMessage") or {}).get("text")
        wa_message_id = key.get("id")
        push_name = data.get("pushName")
        if wa_id:
            contact_id = get_or_create_contact(account["id"], wa_id, push_name)
            chat_id = get_or_create_chat(account["id"], contact_id, default_auto_reply=account.get("ai_auto_reply_enabled", True))

            # Captura de exame/documento — canal lateral que não reordena nem
            # altera a cadeia de prioridade abaixo (confirmação de consultor →
            # "minha agenda" → booking_flow → IA); só roda pra contas em modo
            # CRM médico, pra não acumular arquivo de conta sem esse recurso.
            media = _extract_media_info(message)
            message_type = media["doc_type"] if media else "text"
            message_id = save_message(chat_id, account["id"], "in", body, sender_contact_id=contact_id,
                                      wa_message_id=wa_message_id, message_type=message_type)
            if media and plan_booking_mode(account.get("user_id")) == "crm_medico":
                _enqueue_patient_media(account, contact_id, message_id, key, media, wa_message_id)

            # pending_consultant_id roda sempre (não só quando há selected_id) —
            # a resposta de confirmação aceita tanto tocar no botão quanto
            # digitar sim/não (fallback pro caso do botão nativo não renderizar
            # no aparelho do destinatário, ver plano). answer=None (nem botão
            # nem sim/não reconhecido) cai no fluxo normal mais abaixo.
            pending_consultant_id = get_consultant_by_pending_contact(account["id"], wa_id)
            answer = None
            if selected_id == f"consultant_confirm_{pending_consultant_id}":
                answer = True
            elif selected_id == f"consultant_decline_{pending_consultant_id}":
                answer = False
            elif pending_consultant_id and not selected_id:
                answer = booking_flow.parse_yes_no(body)

            wants_portal_link = bool(body) and "minha agenda" in body.strip().lower()
            active_consultant = get_active_consultant_by_wa_id(account["id"], wa_id) if wants_portal_link else None

            if pending_consultant_id and answer is not None:
                new_status = "active" if answer else "declined"
                set_consultant_status(pending_consultant_id, new_status)
                if new_status == "active":
                    link = portal_link(get_portal_token(pending_consultant_id))
                    reply = f"Cadastro confirmado! Você já pode receber agendamentos.\n\nAcesse sua agenda quando quiser: {link}"
                else:
                    reply = "Ok, cadastro cancelado."
                try:
                    evolution.send_text(account["wa_session_name"], _phone_from_wa_id(wa_id), reply)
                except EvolutionError:
                    pass
            elif active_consultant:
                try:
                    evolution.send_text(account["wa_session_name"], _phone_from_wa_id(wa_id),
                                         f"Sua agenda: {portal_link(active_consultant['portal_token'])}")
                except EvolutionError:
                    pass
            elif booking_flow.handle_incoming(account, chat_id, contact_id, wa_id, body, selected_id, push_name):
                pass  # tratado pelo fluxo de agendamento — não cai na IA nem na medição de recebida-sem-área
            elif account.get("area_id"):
                threading.Thread(
                    target=_handle_ai_auto_reply, args=(account, chat_id, wa_id, body), daemon=True
                ).start()
            else:
                threading.Thread(target=_handle_unrelated_received_usage, args=(account,), daemon=True).start()

    return jsonify({"ok": True})


if __name__ == "__main__":
    migrate_if_needed()
    server_cfg = WHATSAPP_CONFIG.get("server") or {}
    threading.current_thread().name = "whatsapp-agent-main"
    threading.Thread(target=_reminder_loop, daemon=True, name="reminder-loop").start()
    app.run(
        host=server_cfg.get("host", "0.0.0.0"),
        port=server_cfg.get("port", 5005),
        threaded=True,
    )
