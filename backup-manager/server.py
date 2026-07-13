#!/usr/bin/env python3
"""Backup Manager — cadastro de pastas/bancos + backup sob demanda ou agendado"""

import copy
import datetime
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

import psycopg2
from flask import Flask, render_template, jsonify, request

from config import DB_CONFIG, BACKUP_CONFIG, CONFIG, save_config
from db_migrations import migrate_if_needed

app = Flask(__name__)

BACKUP_DIR = BACKUP_CONFIG["backup_dir"]
BROWSE_ROOT = "/root"  # /api/browse nunca lista fora daqui

BACKUP_LOCK = threading.Lock()   # evita dois backups (manual/manual ou manual/automático) rodando ao mesmo tempo
SCHEDULE_LOCK = threading.Lock()  # protege leitura/escrita de SCHEDULE_STATE


def run(cmd, timeout=300):
    """Executa comando shell e retorna (success, output, error)"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "Timeout expirado"


def slugify(label):
    import re
    s = re.sub(r'[^a-z0-9]+', '-', (label or '').lower()).strip('-')
    return s or 'backup'


# ---------------------------------------------------------------------------
# Arquivos de backup em disco
# ---------------------------------------------------------------------------

def list_backups():
    """Lista arquivos de backup com metadados, incluindo o tipo (pasta/banco)
    inferido pela extensão — não dá mais pra inferir pelo nome do projeto
    porque os labels agora são cadastrados livremente."""
    files = []
    db_extensions = ('.dump.gz', '.dump')
    folder_extensions = ('.tar', '.tar.gz', '.zip')
    for f in sorted(Path(BACKUP_DIR).iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and f.name.endswith(db_extensions + folder_extensions):
            stat = f.stat()
            size_mb = stat.st_size / (1024 * 1024)
            files.append({
                "name": f.name,
                "kind": "database" if f.name.endswith(db_extensions) else "folder",
                "size": round(size_mb, 2),
                "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "mtime": stat.st_mtime
            })
    return files


def create_folder_backup(target):
    """Tar completo (sem exclusões) da pasta do alvo cadastrado."""
    path = target["path"]
    if not path or not os.path.isdir(path):
        return False, f"Pasta não encontrada no servidor: {path}"

    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    filename = f"{slugify(target['label'])}-backup-{date_str}.tar"
    filepath = os.path.join(BACKUP_DIR, filename)

    if os.path.exists(filepath):
        os.remove(filepath)

    cmd = f"tar cf {shlex.quote(filepath)} -C {shlex.quote(path)} ."
    success, out, err = run(cmd)
    if not success:
        return False, err

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    return True, f"Backup criado: {filename} ({round(size_mb, 2)}MB)"


def create_db_backup_for(target):
    """Dump compactado de um banco Postgres cadastrado."""
    dbname = target["dbname"]
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    filename = f"{slugify(target['label'])}-backup-{date_str}.dump.gz"
    filepath = os.path.join(BACKUP_DIR, filename)

    if os.path.exists(filepath):
        os.remove(filepath)

    pg_env = f"PGPASSWORD={shlex.quote(DB_CONFIG['password'])} " if DB_CONFIG.get("password") else ""
    cmd = (
        f"{pg_env}pg_dump -U {shlex.quote(DB_CONFIG['user'])} -h {shlex.quote(DB_CONFIG['host'])} "
        f"--format=custom -d {shlex.quote(dbname)} | gzip > {shlex.quote(filepath)}"
    )
    success, out, err = run(cmd, timeout=600)
    if not success:
        return False, err or "Erro desconhecido no pg_dump"

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    return True, f"Backup criado: {filename} ({round(size_mb, 2)}MB)"


def delete_backup(filename):
    """Deleta um arquivo de backup"""
    filepath = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(filepath):
        return False, "Arquivo não encontrado"

    # Segurança: só deleta dentro do BACKUP_DIR
    real_path = Path(filepath).resolve()
    if not str(real_path).startswith(str(Path(BACKUP_DIR).resolve())):
        return False, "Acesso negado — caminho inválido"

    os.remove(filepath)
    return True, f"{filename} deletado com sucesso"


def cleanup_old_backups(retention_days):
    """Apaga backups (de qualquer alvo) mais velhos que retention_days dias.
    retention_days vazio/None/0 = não apaga nada."""
    if not retention_days or retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for f in list_backups():
        if f["mtime"] < cutoff:
            ok, _ = delete_backup(f["name"])
            if ok:
                removed += 1
    return removed


# ---------------------------------------------------------------------------
# Cadastro de alvos (backup_targets) — banco compartilhado ai_tutor_db
# ---------------------------------------------------------------------------

def _conn():
    return psycopg2.connect(**DB_CONFIG)


def get_targets():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, kind, label, path, dbname, auto_backup, enabled, created_at
               FROM backup_targets ORDER BY kind, label"""
        )
        cols = ["id", "kind", "label", "path", "dbname", "auto_backup", "enabled", "created_at"]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = str(r["created_at"])
        return rows
    finally:
        conn.close()


def get_target(target_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, kind, label, path, dbname, auto_backup, enabled FROM backup_targets WHERE id=%s",
            (target_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = ["id", "kind", "label", "path", "dbname", "auto_backup", "enabled"]
        return dict(zip(cols, row))
    finally:
        conn.close()


def create_target(kind, label, path=None, dbname=None, auto_backup=True):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO backup_targets (kind, label, path, dbname, auto_backup)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (kind, label, path, dbname, auto_backup),
        )
        target_id = cur.fetchone()[0]
        conn.commit()
        return target_id
    finally:
        conn.close()


def update_target(target_id, fields):
    if not fields:
        return False
    conn = _conn()
    try:
        cur = conn.cursor()
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(
            f"UPDATE backup_targets SET {set_clause} WHERE id = %s",
            list(fields.values()) + [target_id],
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_target(target_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM backup_targets WHERE id = %s", (target_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_available_databases():
    """Bancos existentes no cluster Postgres (exceto templates) — alimenta o
    <select> de cadastro, evitando digitar um nome errado à mão."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def browse_folder(path):
    """Lista subpastas imediatas de `path`, só leitura, restrito a dentro de
    BROWSE_ROOT — ajuda a escolher um caminho pela tela em vez de digitar às
    cegas, sem expor a árvore inteira do servidor."""
    base = path or BROWSE_ROOT
    real = os.path.realpath(base)
    root_real = os.path.realpath(BROWSE_ROOT)
    if not (real == root_real or real.startswith(root_real + os.sep)):
        return None
    if not os.path.isdir(real):
        return None
    try:
        folders = sorted(
            e for e in os.listdir(real)
            if os.path.isdir(os.path.join(real, e)) and not e.startswith('.')
        )
    except PermissionError:
        folders = []
    return {
        "path": real,
        "parent": os.path.dirname(real) if real != root_real else None,
        "folders": folders,
    }


# ---------------------------------------------------------------------------
# Execução de backup (manual ou agendada) — sempre serializado por BACKUP_LOCK
# ---------------------------------------------------------------------------

def run_full_backup(auto_only=False):
    """Roda backup de todos os alvos habilitados — bancos primeiro (mais
    demorado), depois pastas. auto_only=True restringe aos alvos marcados
    'entra no automático' (usado pelo scheduler; o botão manual 'Backup
    Completo' roda todos os alvos habilitados, com ou sem essa marcação)."""
    targets = [t for t in get_targets() if t["enabled"]]
    if auto_only:
        targets = [t for t in targets if t["auto_backup"]]
    targets.sort(key=lambda t: 0 if t["kind"] == "database" else 1)

    results = []
    for t in targets:
        ok, msg = create_db_backup_for(t) if t["kind"] == "database" else create_folder_backup(t)
        results.append({"name": t["label"], "ok": ok, "message": msg})
    return results


def _run_single_target(target):
    if target["kind"] == "database":
        return create_db_backup_for(target)
    return create_folder_backup(target)


# ---------------------------------------------------------------------------
# Agendamento automático — thread em background, sem dependência nova
# (mesmo espírito do oraculo_monitoragent/monitor/scheduler.py, mas com
# intervalo em horas em vez de expressão cron)
# ---------------------------------------------------------------------------

def _initial_schedule_state():
    sched = (BACKUP_CONFIG.get("schedule") or {})
    last_run_at = None
    backups = list_backups()
    if backups:
        # Semeia com o backup mais recente já existente, pra não disparar um
        # backup automático assim que o serviço reinicia (systemctl restart
        # de rotina não deveria contar como "hora de rodar de novo").
        last_run_at = backups[0]["mtime"]
    return {
        "enabled": bool(sched.get("enabled", False)),
        "interval_hours": int(sched.get("interval_hours") or 24),
        "retention_days": sched.get("retention_days"),
        "last_run_at": last_run_at,
        "last_run_ok": None,
        "last_run_message": None,
    }


SCHEDULE_STATE = _initial_schedule_state()


def run_backup_scheduler():
    while True:
        time.sleep(60)
        try:
            with SCHEDULE_LOCK:
                enabled = SCHEDULE_STATE["enabled"]
                interval_hours = SCHEDULE_STATE["interval_hours"]
                last_run_at = SCHEDULE_STATE["last_run_at"]
                retention_days = SCHEDULE_STATE["retention_days"]

            if not enabled:
                continue
            due = last_run_at is None or (time.time() - last_run_at) >= interval_hours * 3600
            if not due:
                continue

            if not BACKUP_LOCK.acquire(blocking=False):
                continue  # já tem um backup rodando (manual, provavelmente) — tenta de novo no próximo ciclo

            try:
                print("[scheduler] Iniciando backup automático...")
                results = run_full_backup(auto_only=True)
                ok = all(r["ok"] for r in results) if results else True
                msg = "; ".join(f"{r['name']}: {'ok' if r['ok'] else r['message']}" for r in results) or "Nenhum alvo com 'entra no automático' cadastrado"
                if retention_days:
                    removed = cleanup_old_backups(retention_days)
                    msg += f" | {removed} backup(s) antigo(s) removido(s) pela retenção"
                with SCHEDULE_LOCK:
                    SCHEDULE_STATE["last_run_at"] = time.time()
                    SCHEDULE_STATE["last_run_ok"] = ok
                    SCHEDULE_STATE["last_run_message"] = msg
                print(f"[scheduler] Backup automático concluído: {msg}")
            finally:
                BACKUP_LOCK.release()
        except Exception as e:
            print(f"[scheduler] Erro no ciclo automático: {e}")


def _fmt_ts(ts):
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ── Rota principal (HTML) ──
@app.route("/")
def index():
    backups = list_backups()
    return render_template("index.html", backups=backups)


# ── API — arquivos de backup ──
@app.route("/api/backups")
def api_list():
    return jsonify({"backups": list_backups()})


@app.route("/api/backup/delete", methods=["POST"])
def api_delete():
    data = request.json
    filename = data.get("filename")
    if not filename:
        return jsonify({"ok": False, "message": "Filename required"}), 400
    ok, msg = delete_backup(filename)
    status = 200 if ok else 400
    return jsonify({"ok": ok, "message": msg}), status


# ── API — cadastro de alvos ──
@app.route("/api/targets")
def api_list_targets():
    return jsonify({"targets": get_targets()})


@app.route("/api/targets", methods=["POST"])
def api_create_target():
    data = request.json or {}
    kind = data.get("kind")
    label = (data.get("label") or "").strip()
    auto_backup = bool(data.get("auto_backup", True))

    if kind not in ("folder", "database"):
        return jsonify({"ok": False, "message": "kind precisa ser 'folder' ou 'database'"}), 400
    if not label:
        return jsonify({"ok": False, "message": "Label é obrigatório"}), 400

    if kind == "folder":
        path = (data.get("path") or "").strip()
        if not path or not os.path.isdir(path):
            return jsonify({"ok": False, "message": f"Pasta não encontrada no servidor: {path}"}), 400
        real_path = os.path.realpath(path)
        real_backup_dir = os.path.realpath(BACKUP_DIR)
        if real_backup_dir == real_path or real_backup_dir.startswith(real_path + os.sep):
            return jsonify({"ok": False, "message": "Essa pasta contém o diretório de backups dentro dela — escolha outra."}), 400
        target_id = create_target("folder", label, path=path, auto_backup=auto_backup)
    else:
        dbname = (data.get("dbname") or "").strip()
        if dbname not in list_available_databases():
            return jsonify({"ok": False, "message": f"Banco '{dbname}' não existe no servidor"}), 400
        target_id = create_target("database", label, dbname=dbname, auto_backup=auto_backup)

    return jsonify({"ok": True, "id": target_id}), 201


@app.route("/api/targets/<int:target_id>", methods=["PATCH"])
def api_update_target(target_id):
    data = request.json or {}
    fields = {}
    if "label" in data:
        label = (data.get("label") or "").strip()
        if not label:
            return jsonify({"ok": False, "message": "Label não pode ser vazio"}), 400
        fields["label"] = label
    if "auto_backup" in data:
        fields["auto_backup"] = bool(data["auto_backup"])
    if "enabled" in data:
        fields["enabled"] = bool(data["enabled"])
    if not fields:
        return jsonify({"ok": False, "message": "Nada para atualizar"}), 400

    ok = update_target(target_id, fields)
    if not ok:
        return jsonify({"ok": False, "message": "Alvo não encontrado"}), 404
    return jsonify({"ok": True})


@app.route("/api/targets/<int:target_id>", methods=["DELETE"])
def api_delete_target(target_id):
    ok = delete_target(target_id)
    if not ok:
        return jsonify({"ok": False, "message": "Alvo não encontrado"}), 404
    return jsonify({"ok": True})


@app.route("/api/databases/available")
def api_available_databases():
    return jsonify({"databases": list_available_databases()})


@app.route("/api/browse")
def api_browse():
    path = request.args.get("path") or BROWSE_ROOT
    result = browse_folder(path)
    if result is None:
        return jsonify({"error": "Caminho inválido ou fora do escopo permitido"}), 400
    return jsonify(result)


# ── API — disparar backup ──
@app.route("/api/backup/target/<int:target_id>", methods=["POST"])
def api_backup_target(target_id):
    target = get_target(target_id)
    if not target:
        return jsonify({"ok": False, "message": "Alvo não encontrado"}), 404

    if not BACKUP_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "message": "Já existe um backup em andamento — aguarde terminar."}), 409
    try:
        ok, msg = _run_single_target(target)
    finally:
        BACKUP_LOCK.release()
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 500)


@app.route("/api/backup/all", methods=["POST"])
def api_all():
    """Backup completo: todos os alvos habilitados no cadastro."""
    if not BACKUP_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "results": [], "message": "Já existe um backup em andamento — aguarde terminar."}), 409
    try:
        results = run_full_backup(auto_only=False)
    finally:
        BACKUP_LOCK.release()
    all_ok = all(r["ok"] for r in results) if results else True
    return jsonify({"ok": all_ok, "results": results}), 200 if all_ok else 500


# ── API — agendamento e retenção ──
@app.route("/api/schedule")
def api_get_schedule():
    with SCHEDULE_LOCK:
        state = dict(SCHEDULE_STATE)
    next_run_at = None
    if state["enabled"] and state["last_run_at"]:
        next_run_at = state["last_run_at"] + state["interval_hours"] * 3600
    return jsonify({
        "enabled": state["enabled"],
        "interval_hours": state["interval_hours"],
        "retention_days": state["retention_days"],
        "last_run_at": _fmt_ts(state["last_run_at"]),
        "last_run_ok": state["last_run_ok"],
        "last_run_message": state["last_run_message"],
        "next_run_at": _fmt_ts(next_run_at),
    })


@app.route("/api/schedule", methods=["POST"])
def api_save_schedule():
    data = request.json or {}
    enabled = bool(data.get("enabled", False))

    try:
        interval_hours = int(data.get("interval_hours") or 24)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Intervalo em horas inválido"}), 400
    if interval_hours < 1:
        return jsonify({"ok": False, "message": "Intervalo em horas precisa ser >= 1"}), 400

    retention_raw = data.get("retention_days")
    retention_days = None
    if retention_raw not in (None, ""):
        try:
            retention_days = int(retention_raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "Retenção em dias inválida"}), 400
        if retention_days < 1:
            return jsonify({"ok": False, "message": "Retenção em dias precisa ser >= 1 (ou vazio)"}), 400

    with SCHEDULE_LOCK:
        SCHEDULE_STATE["enabled"] = enabled
        SCHEDULE_STATE["interval_hours"] = interval_hours
        SCHEDULE_STATE["retention_days"] = retention_days

    new_config = copy.deepcopy(CONFIG)
    new_config.setdefault("backup_manager", {})["schedule"] = {
        "enabled": enabled,
        "interval_hours": interval_hours,
        "retention_days": retention_days,
    }
    try:
        save_config(new_config)
    except Exception as e:
        return jsonify({"ok": False, "message": f"Aplicado em memória, mas falhou ao gravar config.yaml: {e}"}), 500

    return jsonify({"ok": True})


if __name__ == "__main__":
    migrate_if_needed()
    threading.Thread(target=run_backup_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=5004, threaded=True)
