#!/usr/bin/env python3
"""WhatsApp Agent — serviço independente (porta 5005) pra integração com
WhatsApp (conta comum via QR Code, usando a Evolution API como conector).

Roda separado do Oráculo (ai_oraculo_saas) — não importa nem altera nada lá.
Só consome a API do Oráculo quando fizer sentido (ex: resposta automática via
RAG, planejado pra depois). Por enquanto: cadastro de contas + fluxo de
conexão/QR, primeiro passo do módulo descrito no plano.
"""

import datetime
import re
import threading

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import connectors.evolution as evolution
from config import DB_CONFIG, WHATSAPP_CONFIG
from connectors.evolution import EvolutionError
from db_migrations import migrate_if_needed

app = Flask(__name__, static_folder=None)
CORS(app)

PUBLIC_DIR = "public"


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def slugify(label):
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return s or "conta"


def _instance_name(account_id, label):
    # Nome estável da instância na Evolution API — inclui o id pra nunca colidir
    # entre contas com o mesmo rótulo.
    return f"oraculo-{account_id}-{slugify(label)}"


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
    "evolution_instance_name", "ai_auto_reply_enabled", "last_connected_at", "created_at",
]


def _row_to_account(row):
    d = dict(zip(ACCOUNT_COLUMNS, row))
    for k in ("last_connected_at", "created_at"):
        if d.get(k):
            d[k] = str(d[k])
    return d


def get_accounts():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(ACCOUNT_COLUMNS)} FROM whatsapp_accounts ORDER BY created_at DESC")
        return [_row_to_account(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_account(account_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(ACCOUNT_COLUMNS)} FROM whatsapp_accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
        return _row_to_account(row) if row else None
    finally:
        conn.close()


def create_account(label, connection_type):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO whatsapp_accounts (label, connection_type)
               VALUES (%s, %s) RETURNING id""",
            (label, connection_type),
        )
        account_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE whatsapp_accounts SET evolution_instance_name = %s WHERE id = %s",
            (_instance_name(account_id, label), account_id),
        )
        conn.commit()
        return account_id
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


# ---------------------------------------------------------------------------
# Rotas — página própria
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "whatsapp-agent"})


# ---------------------------------------------------------------------------
# Rotas — API de contas
# ---------------------------------------------------------------------------

@app.route("/api/whatsapp/accounts", methods=["GET"])
def api_list_accounts():
    return jsonify({"accounts": get_accounts()})


@app.route("/api/whatsapp/accounts", methods=["POST"])
def api_create_account():
    data = request.json or {}
    label = (data.get("label") or "").strip()
    connection_type = data.get("connection_type") or "qrcode"
    if not label:
        return jsonify({"ok": False, "message": "Label é obrigatório"}), 400
    if connection_type not in ("qrcode", "business_api"):
        return jsonify({"ok": False, "message": "connection_type inválido"}), 400
    if connection_type == "business_api":
        return jsonify({"ok": False, "message": "Business API ainda não implementado nesta primeira versão — use QR Code."}), 400

    account_id = create_account(label, connection_type)
    log_event(account_id, "account_created", detail={"label": label, "connection_type": connection_type})
    return jsonify({"ok": True, "id": account_id}), 201


@app.route("/api/whatsapp/accounts/<int:account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404
    if account["connection_type"] == "qrcode":
        if account["status"] == "connected":
            try:
                evolution.logout(account["evolution_instance_name"])
            except EvolutionError:
                pass
        try:
            evolution.delete_instance(account["evolution_instance_name"])
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

    instance_name = account["evolution_instance_name"]
    try:
        evolution.create_instance(instance_name)
        qr_data = evolution.get_qr(instance_name)
    except EvolutionError as e:
        update_account_status(account_id, "error")
        log_event(account_id, "connect_failed", level="error", detail={"error": str(e)})
        return jsonify({"ok": False, "message": str(e)}), 502

    qr_base64 = qr_data.get("base64") or qr_data.get("code")
    if not qr_base64:
        # já pode estar conectado (sessão anterior persistida no volume Docker)
        state = evolution.connection_state(instance_name)
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
            state = evolution.connection_state(account["evolution_instance_name"])
        except EvolutionError as e:
            return jsonify({"ok": True, "status": account["status"], "warning": str(e)})

        if state == "open" and account["status"] != "connected":
            update_account_status(account_id, "connected", connected=True)
            mark_session_connected(account_id)
            log_event(account_id, "connected")
            account = get_account(account_id)
        elif state == "close" and account["status"] == "connected":
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
        evolution.logout(account["evolution_instance_name"])
    except EvolutionError as e:
        return jsonify({"ok": False, "message": str(e)}), 502
    update_account_status(account_id, "disconnected")
    log_event(account_id, "disconnected_manual")
    return jsonify({"ok": True})


if __name__ == "__main__":
    migrate_if_needed()
    server_cfg = WHATSAPP_CONFIG.get("server") or {}
    threading.current_thread().name = "whatsapp-agent-main"
    app.run(
        host=server_cfg.get("host", "0.0.0.0"),
        port=server_cfg.get("port", 5005),
        threaded=True,
    )
