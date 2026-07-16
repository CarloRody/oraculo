#!/usr/bin/env python3
"""WhatsApp Agent — serviço independente (porta 5005) pra integração com
WhatsApp (conta comum via QR Code, usando a Evolution API como conector).

Roda separado do Oráculo (ai_oraculo_saas) — não importa nem altera nada lá.
Só consome a API do Oráculo quando faz sentido: o bot de resposta automática
(ai_auto_reply_enabled + conta vinculada a uma área) chama /api/chat de lá
via HTTP, nunca lê o banco dele diretamente pra isso.
"""

import datetime
import re
import secrets
import threading
import time

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import booking_flow
import connectors.evolution as evolution
from config import DB_CONFIG, ORACULO_API_CONFIG, WHATSAPP_CONFIG
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
]


def _row_to_account(row):
    d = dict(zip(ACCOUNT_COLUMNS, row))
    for k in ("last_connected_at", "created_at"):
        if d.get(k):
            d[k] = str(d[k])
    return d


def get_accounts(user_id=None):
    # LEFT JOIN com users e areas (mesmo ai_tutor_db, mesma conexão — não são
    # bancos separados) só pra trazer o e-mail do cliente e o nome da área
    # vinculados junto, sem round-trip extra pro frontend.
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(f"a.{c}" for c in ACCOUNT_COLUMNS)
        where = "WHERE a.user_id = %s" if user_id else ""
        params = (user_id,) if user_id else ()
        cur.execute(
            f"""SELECT {cols}, u.email AS client_email, ar.name AS area_name
                FROM whatsapp_accounts a
                LEFT JOIN users u ON u.id = a.user_id
                LEFT JOIN areas ar ON ar.id = a.area_id
                {where}
                ORDER BY a.created_at DESC""",
            params,
        )
        rows = []
        for r in cur.fetchall():
            d = _row_to_account(r[:-2])
            d["client_email"] = r[-2]
            d["area_name"] = r[-1]
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
            f"""SELECT {cols}, u.email AS client_email, ar.name AS area_name
                FROM whatsapp_accounts a
                LEFT JOIN users u ON u.id = a.user_id
                LEFT JOIN areas ar ON ar.id = a.area_id
                WHERE a.id = %s""",
            (account_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        d = _row_to_account(row[:-2])
        d["client_email"] = row[-2]
        d["area_name"] = row[-1]
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


def save_message(chat_id, account_id, direction, body, sender_contact_id=None, wa_message_id=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        status = "sent" if direction == "out" else "delivered"
        cur.execute(
            """INSERT INTO whatsapp_messages
               (chat_id, account_id, wa_message_id, direction, sender_contact_id, message_type, body, status)
               VALUES (%s, %s, %s, %s, %s, 'text', %s, %s) RETURNING id""",
            (chat_id, account_id, wa_message_id, direction, sender_contact_id, body, status),
        )
        message_id = cur.fetchone()[0]
        preview = (body or "")[:120]
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

def plan_has_agenda(user_id):
    # Leitura direta em plans (mesmo ai_tutor_db, mesma conexão — mesmo
    # padrão já usado em get_clients()/_client_api_key()); nunca escrevemos
    # nessa tabela daqui. Sem cliente vinculado à conta = sem agenda.
    if not user_id:
        return False
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT p.agenda_enabled FROM users u JOIN plans p ON p.id = u.plan_id WHERE u.id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        return bool(row[0]) if row else False
    finally:
        conn.close()


CONSULTANT_COLUMNS = [
    "id", "account_id", "contact_id", "name", "context", "slot_duration_minutes",
    "weekly_availability", "reminder_hours_before", "status", "confirmed_at", "created_at",
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


def get_appointments(account_id, upcoming_only=True):
    conn = _conn()
    try:
        cur = conn.cursor()
        where_extra = "AND a2.scheduled_at >= NOW()" if upcoming_only else ""
        cur.execute(
            f"""SELECT a2.id, a2.consultant_id, c.name, a2.client_contact_id, ct.push_name, ct.wa_id,
                       a2.scheduled_at, a2.duration_minutes, a2.status
                FROM whatsapp_appointments a2
                JOIN whatsapp_consultants c ON c.id = a2.consultant_id
                JOIN whatsapp_contacts ct ON ct.id = a2.client_contact_id
                WHERE c.account_id = %s {where_extra}
                ORDER BY a2.scheduled_at""",
            (account_id,),
        )
        cols = ["id", "consultant_id", "consultant_name", "client_contact_id", "client_name", "client_wa_id",
                "scheduled_at", "duration_minutes", "status"]
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
    """Pro portal do próprio consultor: agenda futura + um histórico curto
    (últimos concluídos/cancelados/passados), separado do get_appointments
    admin (que é por CONTA, não por consultor)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cols_sql = "a.id, a.client_contact_id, ct.push_name, ct.wa_id, a.scheduled_at, a.duration_minutes, a.status"
        cols = ["id", "client_contact_id", "client_name", "client_wa_id", "scheduled_at", "duration_minutes", "status"]

        cur.execute(
            f"""SELECT {cols_sql} FROM whatsapp_appointments a JOIN whatsapp_contacts ct ON ct.id = a.client_contact_id
                WHERE a.consultant_id = %s AND a.status = 'confirmed' AND a.scheduled_at >= NOW()
                ORDER BY a.scheduled_at""",
            (consultant_id,),
        )
        upcoming = [dict(zip(cols, r)) for r in cur.fetchall()]

        cur.execute(
            f"""SELECT {cols_sql} FROM whatsapp_appointments a JOIN whatsapp_contacts ct ON ct.id = a.client_contact_id
                WHERE a.consultant_id = %s AND (a.status != 'confirmed' OR a.scheduled_at < NOW())
                ORDER BY a.scheduled_at DESC LIMIT 10""",
            (consultant_id,),
        )
        history = [dict(zip(cols, r)) for r in cur.fetchall()]

        for lst in (upcoming, history):
            for r in lst:
                r["scheduled_at"] = r["scheduled_at"].astimezone(booking_flow.LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")
        return upcoming, history
    finally:
        conn.close()


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


def _reminder_loop():
    """Único processo em background do whatsapp-agent — não existia nenhum
    antes desta feature (tudo mais é disparado sob demanda por webhook).
    Polling simples (sem dependência nova tipo APScheduler): a cada 5min,
    manda lembrete pro cliente e pro consultor de agendamentos cujo horário
    de aviso chegou."""
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


def _send_consultant_confirmation(consultant_id):
    """Manda o convite com botões Sim/Não pro contato confirmar que aceita
    virar consultor — sem isso o status nunca sai de 'pending_confirmation'
    e ele não aparece nas opções do fluxo de agendamento. Roda em thread
    separada, erro só loga."""
    consultant = get_consultant(consultant_id)
    if not consultant:
        return
    try:
        evolution.send_buttons(
            consultant["wa_session_name"],
            _phone_from_wa_id(consultant["wa_id"]),
            "Convite para ser consultor",
            f"Você foi cadastrado como consultor ({consultant['name']}) pra receber agendamentos por aqui. Confirma o cadastro?",
            [
                {"id": f"consultant_confirm_{consultant_id}", "text": "Sim, confirmar"},
                {"id": f"consultant_decline_{consultant_id}", "text": "Não"},
            ],
        )
    except EvolutionError as e:
        log_event(consultant["account_id"], "consultant_invite_failed", level="error",
                   detail={"error": str(e), "consultant_id": consultant_id})


@app.route("/api/whatsapp/accounts/<int:account_id>/consultants", methods=["GET"])
def api_list_consultants(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    if not plan_has_agenda(account.get("user_id")):
        return jsonify({"ok": False, "message": "Agenda não está ativada no plano deste cliente."}), 403
    return jsonify({"consultants": get_consultants(account_id)})


@app.route("/api/whatsapp/accounts/<int:account_id>/consultants", methods=["POST"])
def api_create_consultant(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    if not plan_has_agenda(account.get("user_id")):
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
    allowed = ("name", "context", "slot_duration_minutes", "weekly_availability", "reminder_hours_before", "status")
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


@app.route("/api/whatsapp/accounts/<int:account_id>/appointments", methods=["GET"])
def api_list_appointments(account_id):
    if not get_account(account_id):
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    return jsonify({"appointments": get_appointments(account_id)})


@app.route("/api/whatsapp/appointments/<int:appointment_id>/cancel", methods=["POST"])
def api_cancel_appointment(appointment_id):
    cancel_appointment(appointment_id)
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
    })


@app.route("/api/consultant-portal/<token>/appointments", methods=["GET"])
def api_portal_appointments(token):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    upcoming, history = get_consultant_appointments(consultant["id"])
    return jsonify({"upcoming": upcoming, "history": history})


@app.route("/api/consultant-portal/<token>/free-slots", methods=["GET"])
def api_portal_free_slots(token):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    slots = booking_flow.compute_free_slots(consultant)
    return jsonify({"slots": [s.isoformat() for s in slots]})


@app.route("/api/consultant-portal/<token>/appointments", methods=["POST"])
def api_portal_create_appointment(token):
    consultant = get_consultant_by_portal_token(token)
    if not consultant:
        return jsonify({"ok": False, "message": "Link inválido ou expirado"}), 404
    data = request.json or {}
    phone = re.sub(r"\D", "", data.get("phone") or "")
    name = (data.get("name") or "").strip()
    scheduled_at_raw = data.get("scheduled_at")
    if not phone or not scheduled_at_raw:
        return jsonify({"ok": False, "message": "Telefone e horário são obrigatórios"}), 400
    try:
        scheduled_at = datetime.datetime.fromisoformat(scheduled_at_raw)
    except ValueError:
        return jsonify({"ok": False, "message": "Horário inválido"}), 400

    wa_id = f"{phone}@s.whatsapp.net"
    client_contact_id = get_or_create_contact(consultant["account_id"], wa_id, name or None)
    ok = booking_flow.book_appointment(consultant, client_contact_id, wa_id, name, scheduled_at, notify_consultant=False)
    if not ok:
        return jsonify({"ok": False, "message": "Esse horário não está mais livre"}), 409
    return jsonify({"ok": True}), 201


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

    base_url = (ORACULO_API_CONFIG.get("base_url") or "http://127.0.0.1:5001").rstrip("/")
    try:
        resp = requests.post(
            f"{base_url}/api/chat",
            headers={"X-Oraculo-Key": api_key},
            json={"message": incoming_text, "area_ids": [account["area_id"]]},
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
            save_message(chat_id, account["id"], "in", body, sender_contact_id=contact_id, wa_message_id=wa_message_id)

            pending_consultant_id = get_consultant_by_pending_contact(account["id"], wa_id) if selected_id else None
            wants_portal_link = bool(body) and "minha agenda" in body.strip().lower()
            active_consultant = get_active_consultant_by_wa_id(account["id"], wa_id) if wants_portal_link else None

            if pending_consultant_id and selected_id in (
                f"consultant_confirm_{pending_consultant_id}", f"consultant_decline_{pending_consultant_id}"
            ):
                new_status = "active" if selected_id.startswith("consultant_confirm_") else "declined"
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
