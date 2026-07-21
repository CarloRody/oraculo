"""Shared configuration loader — reads config.yaml at the monorepo root.

Mesmo padrão de backup-manager/config.py e oraculo_monitoragent/config.py —
fonte única de verdade pras credenciais do banco (compartilhado com os outros
serviços) e as próprias configs deste serviço. Editar ../config.yaml (não
este arquivo) pra mudar valores; reinicia o serviço pra aplicar.
"""

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(data):
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)


CONFIG = load_config()

# database: é compartilhado/top-level no config.yaml (mesmo Postgres dos outros serviços).
DB_CONFIG = {k: v for k, v in CONFIG["database"].items() if v is not None}

WHATSAPP_CONFIG = CONFIG.get("whatsapp_agent") or {}
EVOLUTION_CONFIG = CONFIG.get("evolution_api") or {}
ORACULO_API_CONFIG = CONFIG.get("oraculo_api") or {}
WHATSAPP_MEDIA_CONFIG = WHATSAPP_CONFIG.get("media") or {}
