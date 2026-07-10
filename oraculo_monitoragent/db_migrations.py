"""Database migrations for Monitor Agent API tables."""

import psycopg2

from config import DB_CONFIG


def get_db():
    return psycopg2.connect(**DB_CONFIG)


MIGRATIONS = [
    # 1 — monitor_urls
    """
    CREATE TABLE IF NOT EXISTS monitor_urls (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255),
        url TEXT UNIQUE NOT NULL,
        area_id INTEGER REFERENCES areas(id),
        fetch_mode VARCHAR(20) DEFAULT 'http',
        last_fetched_at TIMESTAMP WITH TIME ZONE,
        last_content_hash TEXT,
        enabled BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT NOW()
    );
    """,

    # 2 — monitor_scans
    """
    CREATE TABLE IF NOT EXISTS monitor_scans (
        id SERIAL PRIMARY KEY,
        url_id INTEGER REFERENCES monitor_urls(id) ON DELETE CASCADE,
        scanned_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        content_hash TEXT,
        status VARCHAR(20),
        change_type VARCHAR(20),
        docs_created INTEGER DEFAULT 0,
        docs_updated INTEGER DEFAULT 0,
        duration_seconds NUMERIC(6,2),
        error_message TEXT
    );
    """,

    # 3 — monitor_extracted_links
    """
    CREATE TABLE IF NOT EXISTS monitor_extracted_links (
        id SERIAL PRIMARY KEY,
        parent_url_id INTEGER REFERENCES monitor_urls(id) ON DELETE CASCADE,
        name VARCHAR(500),
        url TEXT NOT NULL,
        link_type VARCHAR(20) DEFAULT 'pdf',
        content_hash TEXT,
        tutor_doc_id INTEGER,
        last_extracted_at TIMESTAMP WITH TIME ZONE,
        UNIQUE(parent_url_id, url)
    );
    """,

    # 4 — Indexes for performance
    """
    CREATE INDEX IF NOT EXISTS idx_monitor_scans_url_id ON monitor_scans(url_id);
    CREATE INDEX IF NOT EXISTS idx_monitor_scans_status ON monitor_scans(status);
    CREATE INDEX IF NOT EXISTS idx_monitor_urls_enabled ON monitor_urls(enabled);
    CREATE INDEX IF NOT EXISTS idx_monitor_extracted_links_parent ON monitor_extracted_links(parent_url_id);
    """,

    # 5 — Fix CASCADE on foreign keys (so deleting a URL cascades to scans + links)
    """
    -- Drop and recreate FK with ON DELETE CASCADE on monitor_scans
    DO $$
    DECLARE
        constraint_name text;
    BEGIN
        SELECT conname INTO constraint_name FROM pg_constraint
        WHERE conrelid = 'monitor_scans'::regclass AND contype = 'f' AND confrelid = 'monitor_urls'::regclass;
        IF constraint_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE monitor_scans DROP CONSTRAINT %I', constraint_name);
            EXECUTE 'ALTER TABLE monitor_scans ADD CONSTRAINT ' || quote_ident(constraint_name) ||
                    ' FOREIGN KEY (url_id) REFERENCES monitor_urls(id) ON DELETE CASCADE';
        END IF;
    END $$;

    -- Drop and recreate FK with ON DELETE CASCADE on monitor_extracted_links
    DO $$
    DECLARE
        constraint_name text;
    BEGIN
        SELECT conname INTO constraint_name FROM pg_constraint
        WHERE conrelid = 'monitor_extracted_links'::regclass AND contype = 'f' AND confrelid = 'monitor_urls'::regclass;
        IF constraint_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE monitor_extracted_links DROP CONSTRAINT %I', constraint_name);
            EXECUTE 'ALTER TABLE monitor_extracted_links ADD CONSTRAINT ' || quote_ident(constraint_name) ||
                    ' FOREIGN KEY (parent_url_id) REFERENCES monitor_urls(id) ON DELETE CASCADE';
        END IF;
    END $$;
    """,

    # 6 — monitor_crawls / monitor_crawl_pages: sessão de rastreamento recursivo
    # de links (árvore de conhecimento). O conteúdo real fica nos documentos
    # da Tutor (tutor_doc_id) — estas tabelas só controlam a sessão de revisão.
    """
    CREATE TABLE IF NOT EXISTS monitor_crawls (
        id SERIAL PRIMARY KEY,
        root_url_id INTEGER REFERENCES monitor_urls(id) ON DELETE SET NULL,
        root_url TEXT NOT NULL,
        max_depth INTEGER NOT NULL DEFAULT 3,
        same_domain_only BOOLEAN DEFAULT TRUE,
        fetch_mode VARCHAR(20) DEFAULT 'http',
        area_id INTEGER REFERENCES areas(id),
        status VARCHAR(20) DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'completed', 'cancelled')),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        finished_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS monitor_crawl_pages (
        id SERIAL PRIMARY KEY,
        crawl_id INTEGER REFERENCES monitor_crawls(id) ON DELETE CASCADE,
        parent_page_id INTEGER REFERENCES monitor_crawl_pages(id) ON DELETE CASCADE,
        url TEXT NOT NULL,
        title VARCHAR(500),
        depth INTEGER NOT NULL,
        tutor_doc_id INTEGER,
        fetched_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(crawl_id, url)
    );

    CREATE INDEX IF NOT EXISTS idx_monitor_crawl_pages_crawl ON monitor_crawl_pages(crawl_id);
    CREATE INDEX IF NOT EXISTS idx_monitor_crawl_pages_parent ON monitor_crawl_pages(parent_page_id);
    """,
]


def migrate_if_needed():
    """Run all migrations idempotently (IF NOT EXISTS guards)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        for sql in MIGRATIONS:
            print(f"Running migration...")
            cur.execute(sql.strip())
        conn.commit()

        # Verify tables exist
        cur.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='public' AND table_name LIKE 'monitor_%'"""
        )
        tables = [r[0] for r in cur.fetchall()]
        print(f"Monitor tables: {tables}")

    except Exception as e:
        conn.rollback()
        print(f"Migration error: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate_if_needed()
    print("Migrations complete.")
