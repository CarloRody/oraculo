"""Database migrations for Backup Manager's own tables.

Segue o mesmo padrão de oraculo_monitoragent/db_migrations.py — lista de
migrações idempotentes (IF NOT EXISTS), aplicadas na subida do processo.
"""

import psycopg2

from config import DB_CONFIG

# Caminhos dos 2 projetos que já eram backupeados por PROJECTS (server.py,
# antes desta feature) — semeados como alvos já cadastrados pra não quebrar
# quem já usa "Backup Completo" hoje.
_SEED_FOLDERS = [
    ("AI Tutor SaaS", "/root/.openclaw/workspace/projects/oraculo/ai_oraculo_saas"),
    ("Monitor Agent", "/root/.openclaw/workspace/projects/oraculo/oraculo_monitoragent"),
]

# Pastas do monorepo que ficaram de fora do seed original — inseridas de
# forma idempotente (por path) em toda subida, não só quando a tabela está
# vazia, pra cobrir quem já tinha o serviço rodando antes dessas pastas
# existirem.
_ADDITIONAL_FOLDERS = [
    ("Backup Manager", "/root/.openclaw/workspace/projects/oraculo/backup-manager"),
    ("WhatsApp Agent", "/root/.openclaw/workspace/projects/oraculo/whatsapp-agent"),
    ("Evolution API", "/root/.openclaw/workspace/projects/oraculo/evolution-api"),
]


def get_db():
    return psycopg2.connect(**DB_CONFIG)


MIGRATIONS = [
    # 1 — backup_targets: cadastro de pastas/bancos disponíveis pra backup
    """
    CREATE TABLE IF NOT EXISTS backup_targets (
        id SERIAL PRIMARY KEY,
        kind VARCHAR(20) NOT NULL CHECK (kind IN ('folder', 'database')),
        label VARCHAR(255) NOT NULL,
        path TEXT,
        dbname TEXT,
        auto_backup BOOLEAN NOT NULL DEFAULT TRUE,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
]


def _seed_if_empty(cur):
    """Insere os 2 projetos + o banco padrão como alvos já cadastrados, só na
    primeira vez (tabela vazia) — preserva o comportamento de hoje sem exigir
    que o usuário recadastre o que já funcionava."""
    cur.execute("SELECT count(*) FROM backup_targets")
    if cur.fetchone()[0] > 0:
        return
    for label, path in _SEED_FOLDERS:
        cur.execute(
            "INSERT INTO backup_targets (kind, label, path) VALUES ('folder', %s, %s)",
            (label, path),
        )
    cur.execute(
        "INSERT INTO backup_targets (kind, label, dbname) VALUES ('database', %s, %s)",
        ("Banco Principal", DB_CONFIG["dbname"]),
    )


def _seed_additional_folders(cur):
    """Insere as pastas de _ADDITIONAL_FOLDERS que ainda não estejam
    cadastradas (por path) — roda em toda subida, independente do estado
    atual da tabela, sem duplicar o que já existe (seed original ou
    cadastro manual pela UI)."""
    for label, path in _ADDITIONAL_FOLDERS:
        cur.execute("SELECT 1 FROM backup_targets WHERE path = %s", (path,))
        if cur.fetchone():
            continue
        cur.execute(
            "INSERT INTO backup_targets (kind, label, path) VALUES ('folder', %s, %s)",
            (label, path),
        )


def migrate_if_needed():
    conn = get_db()
    try:
        cur = conn.cursor()
        for sql in MIGRATIONS:
            cur.execute(sql.strip())
        _seed_if_empty(cur)
        _seed_additional_folders(cur)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Migration error: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate_if_needed()
    print("Migrations complete.")
