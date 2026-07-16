"""Database migrations for AI Tutor SaaS tables.

Mirrors the pattern used in oraculo_monitoragent/db_migrations.py: an ordered
list of idempotent SQL blocks (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS),
safe to run on every startup and safe against both a fresh database and the
already-running production one. Never drops or alters existing data.
"""

import psycopg2

from config import DB_CONFIG


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

    # 7 — fetch_mode on documents (http vs js_browser, mirrors oraculo_monitoragent's urls.fetch_mode)
    """
    ALTER TABLE documents ADD COLUMN IF NOT EXISTS fetch_mode VARCHAR(20) DEFAULT 'http';

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'documents_fetch_mode_check'
        ) THEN
            ALTER TABLE documents ADD CONSTRAINT documents_fetch_mode_check
                CHECK (fetch_mode IN ('http', 'js_browser'));
        END IF;
    END $$;
    """,

    # 8 — parent_doc_id on documents, for link-tree crawls (Monitor Agent)
    """
    ALTER TABLE documents ADD COLUMN IF NOT EXISTS parent_doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL;
    CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents(parent_doc_id);
    """,

    # 9 — subscription plans: reusable quota/price templates per area, live-linked
    # to users via users.plan_id (replaces per-user area_subscriptions editing).
    """
    CREATE TABLE IF NOT EXISTS plans (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL UNIQUE,
        description TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS plan_area_pricing (
        id SERIAL PRIMARY KEY,
        plan_id INTEGER REFERENCES plans(id) ON DELETE CASCADE,
        area_id INTEGER REFERENCES areas(id) ON DELETE CASCADE,
        monthly_token_quota INTEGER,
        price_per_1k_tokens NUMERIC(10,4),
        UNIQUE(plan_id, area_id)
    );

    ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL;

    CREATE INDEX IF NOT EXISTS idx_plan_area_pricing_plan ON plan_area_pricing(plan_id);
    """,

    # 10 — owner_user_id on areas: bases de conhecimento privadas por cliente.
    # NULL = área global/compartilhada (comportamento de sempre); preenchida =
    # área exclusiva daquele cliente (ver /api/my-area e meu-portal.html).
    """
    ALTER TABLE areas ADD COLUMN IF NOT EXISTS owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
    CREATE INDEX IF NOT EXISTS idx_areas_owner ON areas(owner_user_id);
    """,

    # 11 — sistema de créditos pré-pagos: cadastro de modelos de IA (com preço
    # em R$/1M tokens + markup, um modelo real por plano define qual API é
    # chamada), saldo em users.balance e o ledger de depósitos/consumo.
    """
    CREATE TABLE IF NOT EXISTS ai_models (
        id SERIAL PRIMARY KEY,
        name VARCHAR(120) NOT NULL,
        base_url TEXT NOT NULL,
        api_key TEXT,
        model_name VARCHAR(120) NOT NULL,
        temperature NUMERIC(3,2),
        max_tokens INTEGER,
        timeout_seconds INTEGER,
        price_input_per_million NUMERIC(12,4) NOT NULL DEFAULT 0,
        price_output_per_million NUMERIC(12,4) NOT NULL DEFAULT 0,
        markup_percentage NUMERIC(6,2) NOT NULL DEFAULT 0,
        status VARCHAR(10) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    ALTER TABLE plans ADD COLUMN IF NOT EXISTS model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS balance NUMERIC(12,4) NOT NULL DEFAULT 0;

    CREATE TABLE IF NOT EXISTS credit_transactions (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        type VARCHAR(12) NOT NULL CHECK (type IN ('deposit', 'consumption', 'adjustment')),
        amount NUMERIC(12,4) NOT NULL,
        balance_after NUMERIC(12,4) NOT NULL,
        description TEXT,
        session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
        tokens_input INTEGER,
        tokens_output INTEGER,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_credit_transactions_user_time ON credit_transactions(user_id, created_at DESC);
    """,

    # 12 — controle de acesso a páginas: lista única e global de páginas que
    # clientes (identificados por X-Oraculo-Key) podem abrir. Sem chave de
    # cliente salva no navegador = admin/uso interno, acesso total (sem
    # mudança); checagem é feita no navegador por access-guard.js.
    """
    CREATE TABLE IF NOT EXISTS client_allowed_pages (
        page VARCHAR(100) PRIMARY KEY
    );
    """,

    # 13 — multiplicador de preço por feature: permite cobrar mais caro por
    # um recurso premium (ex: Pesquisa 3 PRO High, que faz 3 chamadas de LLM)
    # sem mudar o preço base do modelo usado no chat normal. Default 1.00 =
    # sem sobretaxa (comportamento de sempre pra quem não mexer nisso).
    """
    ALTER TABLE ai_models ADD COLUMN IF NOT EXISTS pro_high_multiplier NUMERIC(5,2) NOT NULL DEFAULT 1.00;
    """,

    # 14 — controle de acesso por CLIENTE, não mais uma lista global aplicada
    # a todo mundo com chave (inclusive o próprio dono da conta, quando ele
    # usa a própria chave de cliente). access_restricted=false (padrão pra
    # clientes novos) = sem restrição configurada ainda, acesso total; true =
    # só as páginas marcadas em client_allowed_pages pra aquele user_id.
    # Diferente do resto das migrações deste arquivo, esta reestrutura dados
    # existentes de propósito (pedido explícito) — o backfill abaixo replica
    # a lista global que existia pra cada cliente já cadastrado, preservando
    # o acesso que cada um já tinha até aqui.
    """
    ALTER TABLE users ADD COLUMN IF NOT EXISTS access_restricted BOOLEAN NOT NULL DEFAULT FALSE;

    ALTER TABLE client_allowed_pages ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;

    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'client_allowed_pages_pkey') THEN
            ALTER TABLE client_allowed_pages DROP CONSTRAINT client_allowed_pages_pkey;
        END IF;
    END $$;

    INSERT INTO client_allowed_pages (user_id, page)
    SELECT u.id, cap.page
    FROM users u
    CROSS JOIN (SELECT page FROM client_allowed_pages WHERE user_id IS NULL) cap;

    UPDATE users SET access_restricted = TRUE
    WHERE EXISTS (SELECT 1 FROM client_allowed_pages WHERE user_id IS NULL);

    DELETE FROM client_allowed_pages WHERE user_id IS NULL;

    ALTER TABLE client_allowed_pages ALTER COLUMN user_id SET NOT NULL;

    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'client_allowed_pages_user_page_pkey') THEN
            ALTER TABLE client_allowed_pages ADD CONSTRAINT client_allowed_pages_user_page_pkey PRIMARY KEY (user_id, page);
        END IF;
    END $$;

    CREATE INDEX IF NOT EXISTS idx_client_allowed_pages_user ON client_allowed_pages(user_id);
    """,

    # 15 — desativação manual de cliente, independente de saldo/plano/modelo.
    # 'active' (padrão) = comportamento de sempre; 'inactive' = bloqueia as
    # APIs de pesquisa (/api/chat, /api/agent-research) e a navegação em
    # qualquer página, ligado/desligado pelo admin na aba Clientes.
    """
    ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(10) NOT NULL DEFAULT 'active';

    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_status_check') THEN
            ALTER TABLE users ADD CONSTRAINT users_status_check CHECK (status IN ('active', 'inactive'));
        END IF;
    END $$;
    """,

    # 16 — prompt personalizado por área. NULL = comportamento de sempre
    # (prompt padrão hardcoded em /api/chat e /api/agent-research); preenchido
    # = instruções extras daquela área (ex: "sempre oriente abertura de
    # chamado"), usadas tanto no chat/pesquisa do site quanto no bot de
    # resposta automática do WhatsApp (whatsapp-agent, área vinculada via
    # whatsapp_accounts.area_id).
    """
    ALTER TABLE areas ADD COLUMN IF NOT EXISTS custom_prompt TEXT;
    """,

    # 17 — API pública de WhatsApp cobrada (cliente manda mensagem via API,
    # usando a conexão vinculada à área) + medição de mensagens recebidas em
    # conexões sem área vinculada. price_per_message_sent é por (plano, área)
    # — mesma granularidade de price_per_1k_tokens; sem preço configurado =
    # envio bloqueado (checado em /api/whatsapp/send, não aqui). Preço/opção
    # de cobrar recebidas-sem-área fica em plans (não tem área pra pendurar).
    """
    ALTER TABLE plan_area_pricing ADD COLUMN IF NOT EXISTS price_per_message_sent NUMERIC(10,4);
    ALTER TABLE plans ADD COLUMN IF NOT EXISTS charge_unrelated_received_messages BOOLEAN NOT NULL DEFAULT FALSE;
    ALTER TABLE plans ADD COLUMN IF NOT EXISTS price_per_unrelated_message NUMERIC(10,4);

    CREATE TABLE IF NOT EXISTS whatsapp_message_usage (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        area_id INTEGER REFERENCES areas(id) ON DELETE SET NULL,
        direction VARCHAR(10) NOT NULL CHECK (direction IN ('sent', 'received')),
        price_charged NUMERIC(10,4),
        wa_account_id INTEGER,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_whatsapp_message_usage_user_time ON whatsapp_message_usage(user_id, created_at DESC);
    """,

    # 18 — toggle de agenda de consultores por plano (feature liga/desliga,
    # sem preço associado). Os dados de agenda em si (consultores,
    # disponibilidade, agendamentos) moram no whatsapp-agent, que lê esta
    # coluna direto via SQL (mesmo padrão de get_clients()/_client_api_key()).
    """
    ALTER TABLE plans ADD COLUMN IF NOT EXISTS agenda_enabled BOOLEAN NOT NULL DEFAULT FALSE;
    """,

    # 19 — orçamento de contexto de conversa (memória) por plano, em tokens.
    # NULL/0 = sem contexto (comportamento de sempre). Separado entre
    # WhatsApp e Pesquisas porque são conversas de natureza diferente
    # (WhatsApp manda o histórico bruto da conversa; Pesquisas reaproveita
    # a sessão do dia já gravada em sessions/messages).
    """
    ALTER TABLE plans ADD COLUMN IF NOT EXISTS whatsapp_context_tokens INTEGER;
    ALTER TABLE plans ADD COLUMN IF NOT EXISTS pesquisa_context_tokens INTEGER;
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
