"""Database migrations for AI Tutor SaaS tables.

Mirrors the pattern used in oraculo_monitoragent/db_migrations.py: an ordered
list of idempotent SQL blocks (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS),
safe to run on every startup and safe against both a fresh database and the
already-running production one. Never drops or alters existing data.
"""

import psycopg2

DB_CONFIG = {
    "dbname": "ai_tutor_db",
    "user": "postgres",
    "host": "/var/run/postgresql",
}


def get_db():
    return psycopg2.connect(**DB_CONFIG)


MIGRATIONS = [
    # 1 — base schema (safe on a fresh database; no-op if already applied)
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email VARCHAR(255) UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role VARCHAR(10) DEFAULT 'user' CHECK (role IN ('admin', 'user')),
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS areas (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        slug VARCHAR(50) UNIQUE NOT NULL,
        vector_ref TEXT NOT NULL,
        status VARCHAR(10) DEFAULT 'draft' CHECK (status IN ('active', 'draft', 'archived'))
    );

    CREATE TABLE IF NOT EXISTS documents (
        id SERIAL PRIMARY KEY,
        area_id INTEGER REFERENCES areas(id),
        name VARCHAR(255) NOT NULL,
        url TEXT,
        content_text TEXT,
        is_external_link BOOLEAN DEFAULT FALSE,
        status VARCHAR(10) DEFAULT 'active' CHECK (status IN ('active', 'stale', 'invalid')),
        last_checked_at TIMESTAMPTZ,
        upload_date TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS document_chunks (
        id SERIAL PRIMARY KEY,
        doc_id INTEGER REFERENCES documents(id),
        area_id INTEGER,
        content_chunk TEXT NOT NULL,
        chunk_index SMALLINT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        area_id INTEGER REFERENCES areas(id),
        title TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        session_id INTEGER REFERENCES sessions(id),
        role VARCHAR(10) CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        token_count INTEGER DEFAULT 0,
        timestamp TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS area_subscriptions (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        area_id INTEGER REFERENCES areas(id),
        status VARCHAR(10) DEFAULT 'active' CHECK (status IN ('active', 'expired')),
        expires_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS usage_logs (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        session_id INTEGER REFERENCES sessions(id),
        tokens_input INTEGER,
        tokens_output INTEGER,
        timestamp TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # 2 — RAG processing tracking columns on documents (added 2026-06/07, undocumented until now)
    """
    ALTER TABLE documents ADD COLUMN IF NOT EXISTS extracted_text TEXT;
    ALTER TABLE documents ADD COLUMN IF NOT EXISTS processing_status VARCHAR(20) DEFAULT 'pending';
    ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_count INTEGER DEFAULT 0;
    ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_processed_at TIMESTAMPTZ;

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'documents_processing_status_check'
        ) THEN
            ALTER TABLE documents ADD CONSTRAINT documents_processing_status_check
                CHECK (processing_status IN ('pending', 'processing', 'indexed', 'failed'));
        END IF;
    END $$;
    """,

    # 3 — embedding + chunk metadata columns on document_chunks (added 2026-06/07, undocumented until now)
    """
    ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_vector TEXT;
    ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS chunk_hash TEXT;
    ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ DEFAULT NOW();
    """,

    # 4 — indexes for performance and search
    """
    CREATE INDEX IF NOT EXISTS idx_documents_area ON documents(area_id);
    CREATE INDEX IF NOT EXISTS idx_docs_url ON documents(url);
    CREATE INDEX IF NOT EXISTS idx_document_chunks_docid ON document_chunks(doc_id);
    CREATE INDEX IF NOT EXISTS idx_chunks_content_fts ON document_chunks
        USING gin (to_tsvector('portuguese', content_chunk));
    CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
    CREATE INDEX IF NOT EXISTS idx_usage_logs_user ON usage_logs(user_id);
    """,

    # 5 — identity, quota, and price columns for per-client token billing
    """
    ALTER TABLE users ADD COLUMN IF NOT EXISTS api_key VARCHAR(64) UNIQUE;
    CREATE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key);

    ALTER TABLE area_subscriptions ADD COLUMN IF NOT EXISTS monthly_token_quota INTEGER;
    ALTER TABLE area_subscriptions ADD COLUMN IF NOT EXISTS price_per_1k_tokens NUMERIC(10,4);

    ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS area_id INTEGER REFERENCES areas(id);
    """,

    # 6 — indexes for quota checks and usage reporting queries
    """
    CREATE INDEX IF NOT EXISTS idx_usage_logs_area ON usage_logs(area_id);
    CREATE INDEX IF NOT EXISTS idx_usage_logs_timestamp ON usage_logs(timestamp);
    CREATE INDEX IF NOT EXISTS idx_usage_logs_user_area_time ON usage_logs(user_id, area_id, timestamp);
    """,
]


def migrate_if_needed():
    """Run all migrations idempotently. Never drops tables, columns, or data."""
    conn = get_db()
    try:
        cur = conn.cursor()
        for sql in MIGRATIONS:
            cur.execute(sql)
        conn.commit()
        print("Migrations applied (idempotent).")
    except Exception as e:
        conn.rollback()
        print(f"Migration error: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate_if_needed()
    print("Migrations complete.")
