"""URL registry — CRUD operations for monitored URLs."""

import psycopg2


def get_db():
    import yaml
    from pathlib import Path
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    return psycopg2.connect(**cfg["database"])


def list_urls(enabled_only: bool = True):
    conn = get_db()
    try:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if enabled_only:
            cur.execute("SELECT * FROM monitor_urls WHERE enabled ORDER BY name")
        else:
            cur.execute("SELECT * FROM monitor_urls ORDER BY name")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_url(name, url, area_id=None, fetch_mode="http", enabled=True):
    conn = get_db()
    try:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """INSERT INTO monitor_urls (name, url, area_id, fetch_mode, enabled)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
            (name, url, area_id, fetch_mode, enabled),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise ValueError(f"URL already registered: {url}")
    finally:
        conn.close()


def update_url(url_id, **kwargs):
    """Update fields of a URL entry. Returns updated row or None."""
    updates = []
    params = []
    idx = 1
    for key, value in kwargs.items():
        if value is not None:
            updates.append(f"{key} = %${idx}")
            params.append(value)
            idx += 1

    if not updates:
        return None

    params.append(url_id)
    sql = f"UPDATE monitor_urls SET {', '.join(updates)} WHERE id = %${idx} RETURNING *"

    conn = get_db()
    try:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def get_url(url_id):
    """Get a single URL entry by ID."""
    conn = get_db()
    try:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM monitor_urls WHERE id = %s", (url_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
