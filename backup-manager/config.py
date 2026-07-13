"""Shared configuration loader — reads config.yaml at the monorepo root.

Single source of truth for DB credentials (shared with the Tutor and Monitor
Agent) and this service's own settings. Edit ../config.yaml (not this file)
to change values; restart the service to pick up changes.
"""

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(data):
    """Grava `data` em config.yaml, substituindo o conteúdo inteiro do arquivo
    (mesmo padrão de ai_oraculo_saas/config.py). Usada pelas configurações de
    agendamento de backup — ao contrário do fluxo de config.yaml do admin
    principal, aqui o processo atualiza seu próprio estado em memória na hora
    (ver SCHEDULE_STATE em server.py), sem precisar reiniciar o serviço."""
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)


CONFIG = load_config()

# database: is shared/top-level in config.yaml (same Postgres as the other services).
DB_CONFIG = {k: v for k, v in CONFIG["database"].items() if v is not None}

BACKUP_CONFIG = CONFIG["backup_manager"]
