"""Shared configuration loader — reads config.yaml at the monorepo root.

Single source of truth for DB credentials and LLM provider settings, used by
server.py, rag_engine.py, migrations.py, and admin_server.py instead of each
hardcoding its own copy. Edit ../config.yaml (not this file) to change values;
restart the service to pick up changes.
"""

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


CONFIG = load_config()

# Drop null values (password/port) so psycopg2 falls back to its defaults —
# a bare {"host": "/var/run/postgresql", "dbname": ..., "user": ...} connects
# via Unix socket peer auth, exactly like the old hardcoded DB_CONFIG did.
DB_CONFIG = {k: v for k, v in CONFIG["database"].items() if v is not None}
