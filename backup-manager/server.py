#!/usr/bin/env python3
"""Backup Manager — Gerenciamento de backups do AI Tutor e Monitor Agent"""

import os
import subprocess
import datetime
from pathlib import Path
from threading import Thread
from flask import Flask, render_template, jsonify, request

from config import DB_CONFIG, BACKUP_CONFIG

app = Flask(__name__)

BACKUP_DIR = BACKUP_CONFIG["backup_dir"]

# Projetos configuráveis: nome exibido, caminho base, arquivos/pastas pra incluir no tar
PROJECTS = {
    "ai-tutor-saas": {
        "label": "AI Tutor SaaS",
        "path": "/root/.openclaw/workspace/projects/oraculo/ai_oraculo_saas",
        "include": [
            "api/", "frontend/", "docs/", "./*.py", "./*.sql", "*.md"
        ],
        "tar_opts": "--exclude=.venv --exclude=__pycache__ --exclude=*.pyc"
    },
    "monitor-agent": {
        "label": "Monitor Agent",
        "path": "/root/.openclaw/workspace/projects/oraculo/oraculo_monitoragent",
        "include": [
            "main.py", "config.yaml", "rag_wrapper.py", "db_migrations.py",
            "seed_urls.py", "README.md", "scraper/", "monitor/", "public/"
        ],
        "tar_opts": "--exclude=.venv --exclude=__pycache__"
    }
}

DATABASE_NAME = DB_CONFIG["dbname"]


def run(cmd, timeout=300):
    """Executa comando shell e retorna (success, output, error)"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "Timeout expirado"


def list_backups():
    """Lista arquivos de backup com metadados"""
    files = []
    extensions = ('.tar', '.tar.gz', '.zip', '.dump.gz', '.dump')
    for f in sorted(Path(BACKUP_DIR).iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and any(f.name.endswith(ext) for ext in extensions):
            stat = f.stat()
            size_mb = stat.st_size / (1024 * 1024)
            files.append({
                "name": f.name,
                "size": round(size_mb, 2),
                "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "mtime": stat.st_mtime
            })
    return files


def create_tar_backup(project_key):
    """Cria backup tar de um projeto"""
    project = PROJECTS[project_key]
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    filename = f"{project_key}-backup-{date_str}.tar"

    # Remove backup do dia se já existe
    existing = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(existing):
        os.remove(existing)

    includes = " ".join(project["include"])
    opts = project.get("tar_opts", "")
    cmd = f"cd {project['path']} && tar cf {existing} {opts} {includes}"

    success, out, err = run(cmd)
    if not success:
        return False, err

    size_mb = os.path.getsize(existing) / (1024 * 1024)
    return True, f"Backup criado: {filename} ({round(size_mb, 2)}MB)"


def create_db_backup():
    """Cria backup compactado do banco PostgreSQL"""
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    filename = f"ai-tutor-db-backup-{date_str}.dump.gz"
    filepath = os.path.join(BACKUP_DIR, filename)

    # Remove se já existe do dia
    if os.path.exists(filepath):
        os.remove(filepath)

    pg_env = f"PGPASSWORD={DB_CONFIG['password']} " if DB_CONFIG.get("password") else ""
    cmd = f"{pg_env}pg_dump -U {DB_CONFIG['user']} -h {DB_CONFIG['host']} --format=custom -d {DATABASE_NAME} | gzip > {filepath}"
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
    if not str(real_path).startswith(Path(BACKUP_DIR).resolve()):
        return False, "Acesso negado — caminho inválido"

    os.remove(filepath)
    return True, f"{filename} deletado com sucesso"


# ── Rota principal (HTML) ──
@app.route("/")
def index():
    backups = list_backups()
    projects = {k: v["label"] for k, v in PROJECTS.items()}
    return render_template("index.html", backups=backups, projects=projects)


# ── API endpoints ──
@app.route("/api/backups")
def api_list():
    return jsonify({"backups": list_backups()})

@app.route("/api/backup/tar/<project_key>", methods=["POST"])
def api_tar(project_key):
    if project_key not in PROJECTS:
        return jsonify({"ok": False, "message": f"Projeto '{project_key}' não encontrado"}), 400
    ok, msg = create_tar_backup(project_key)
    status = 200 if ok else 500
    return jsonify({"ok": ok, "message": msg}), status

@app.route("/api/backup/db", methods=["POST"])
def api_db():
    ok, msg = create_db_backup()
    status = 200 if ok else 500
    return jsonify({"ok": ok, "message": msg}), status

@app.route("/api/backup/all", methods=["POST"])
def api_all():
    """Backup completo: banco + todos os projetos"""
    results = []
    # Banco primeiro (mais demorado)
    ok_db, msg_db = create_db_backup()
    results.append({"name": "Banco PostgreSQL", "ok": ok_db, "message": msg_db})

    for key in PROJECTS:
        ok, msg = create_tar_backup(key)
        results.append({"name": PROJECTS[key]["label"], "ok": ok, "message": msg})

    all_ok = all(r["ok"] for r in results)
    return jsonify({"ok": all_ok, "results": results}), 200 if all_ok else 500

@app.route("/api/backup/delete", methods=["POST"])
def api_delete():
    data = request.json
    filename = data.get("filename")
    if not filename:
        return jsonify({"ok": False, "message": "Filename required"}), 400
    ok, msg = delete_backup(filename)
    status = 200 if ok else 400
    return jsonify({"ok": ok, "message": msg}), status


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5004, threaded=True)
