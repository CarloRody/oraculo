#!/usr/bin/env python3
"""WhatsApp Agent — serviço independente (porta 5005) pra integração com
WhatsApp (conta comum via QR Code, usando a WAHA como conector).

Roda separado do Oráculo (ai_oraculo_saas) — não importa nem altera nada lá.
Só consome a API do Oráculo quando fizer sentido (ex: resposta automática via
RAG, planejado pra depois). Por enquanto: cadastro de contas + fluxo de
conexão/QR, primeiro passo do módulo descrito no plano.
"""

import datetime
import re
import threading
import time

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import connectors.waha as waha
from config import DB_CONFIG, WHATSAPP_CONFIG
from connectors.waha import WahaError
from db_migrations import migrate_if_needed

# WAHA usa nomes de status próprios — mapeados pro enum de whatsapp_accounts.status
WAHA_STATUS_MAP = {
    "STARTING": "connecting",
    "SCAN_QR_CODE": "qr_pending",
    "WORKING": "connected",
    "FAILED": "error",
    "STOPPED": "disconnected",
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
    # Nome estável da sessão na WAHA — inclui o id pra nunca colidir entre
    # contas com o mesmo rótulo.
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
    "wa_session_name", "ai_auto_reply_enabled", "last_connected_at", "created_at",
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
            "UPDATE whatsapp_accounts SET wa_session_name = %s WHERE id = %s",
            (_session_name(account_id, label), account_id),
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
        try:
            waha.delete_session(account["wa_session_name"])
        except WahaError:
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

    session_name = account["wa_session_name"]
    try:
        waha.create_and_start_session(session_name)

        # A WAHA cria a sessão de forma assíncrona (STARTING -> SCAN_QR_CODE),
        # então esperamos um pouco antes do QR ficar disponível — até 20s,
        # checando a cada 1s (a criação em si costuma levar uns 15-18s no Pi).
        status = "STARTING"
        for _ in range(20):
            status = waha.session_status(session_name)
            if status != "STARTING":
                break
            time.sleep(1)
    except WahaError as e:
        update_account_status(account_id, "error")
        log_event(account_id, "connect_failed", level="error", detail={"error": str(e)})
        return jsonify({"ok": False, "message": str(e)}), 502

    our_status = WAHA_STATUS_MAP.get(status, "error")

    if status == "WORKING":
        update_account_status(account_id, "connected", connected=True)
        log_event(account_id, "reconnected_without_qr")
        return jsonify({"ok": True, "status": "connected"})

    if status != "SCAN_QR_CODE":
        update_account_status(account_id, our_status)
        log_event(account_id, "connect_failed", level="error", detail={"waha_status": status})
        return jsonify({"ok": False, "message": f"Sessão não ficou pronta pro QR (status: {status})"}), 502

    try:
        qr_data = waha.get_qr(session_name)
    except WahaError as e:
        update_account_status(account_id, "error")
        return jsonify({"ok": False, "message": str(e)}), 502

    qr_base64 = qr_data.get("data")
    mimetype = qr_data.get("mimetype", "image/png")
    save_qr_session(account_id, qr_base64)
    update_account_status(account_id, "qr_pending")
    log_event(account_id, "qr_generated")
    return jsonify({
        "ok": True,
        "status": "qr_pending",
        "qr_base64": f"data:{mimetype};base64,{qr_base64}",
    })


@app.route("/api/whatsapp/accounts/<int:account_id>/status", methods=["GET"])
def api_account_status(account_id):
    account = get_account(account_id)
    if not account:
        return jsonify({"ok": False, "message": "Conta não encontrada"}), 404

    if account["connection_type"] == "qrcode" and account["status"] in ("qr_pending", "connecting", "connected"):
        try:
            waha_status = waha.session_status(account["wa_session_name"])
        except WahaError as e:
            return jsonify({"ok": True, "account": account, "warning": str(e)})

        our_status = WAHA_STATUS_MAP.get(waha_status, "error")
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
        waha.stop_session(account["wa_session_name"])
    except WahaError as e:
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
