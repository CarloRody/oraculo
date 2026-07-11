"""Shared configuration loader — reads config.yaml at the monorepo root.

Single source of truth for DB credentials (shared with the Tutor) and this
service's own settings, used by main.py, db_migrations.py, and
monitor/url_registry.py instead of each re-reading the file independently.
Edit ../config.yaml (not this file) to change values; restart the service to
pick up changes.
"""

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


CONFIG = load_config()

# database: is shared/top-level in config.yaml (same Postgres as the Tutor).
DB_CONFIG = {k: v for k, v in CONFIG["database"].items() if v is not None}

# This service's own settings live under monitor_agent: to avoid colliding
# with the other services' ports/settings in the shared file.
MONITOR_CONFIG = CONFIG["monitor_agent"]
