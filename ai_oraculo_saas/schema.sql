-- AI Tutor SaaS - Estrutura do Banco de Dados (PostgreSQL)
-- Mantido em sincronia com migrations.py, que aplica isso automaticamente no startup
-- (idempotente — nunca faz DROP). Para mudanças futuras de schema, adicione uma nova
-- entrada em MIGRATIONS (migrations.py) e reflita aqui.

-- 1. Tabela: areas (Engenharia, Matemática, etc.)
CREATE TABLE IF NOT EXISTS areas (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    slug VARCHAR(50) UNIQUE NOT NULL,
    vector_ref TEXT NOT NULL, -- Nome do índice no ChromaDB/Qdrant (ex: 'area_engenharia_v1')
    status VARCHAR(10) DEFAULT 'draft' CHECK (status IN ('active', 'draft', 'archived')),
    owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL -- NULL = área global; preenchida = base de conhecimento privada de um cliente
);

-- 1.5. Tabela: ai_models (Cadastro de modelos de IA — cada linha é um backend
-- OpenAI-compatible real, chamável de fato; preço em R$ por 1M tokens, no
-- padrão OpenRouter, + markup aplicado sobre o custo pra chegar no valor
-- cobrado do cliente)
CREATE TABLE IF NOT EXISTS ai_models (
    id SERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL, -- Nome de exibição, ex "GPT-4o mini (OpenRouter)"
    base_url TEXT NOT NULL, -- Endpoint chat/completions compatível OpenAI
    api_key TEXT, -- Pode ser nulo (modelo local sem auth)
    model_name VARCHAR(120) NOT NULL, -- Valor enviado no campo "model" do body
    temperature NUMERIC(3,2), -- NULL = usa default de config.yaml
    max_tokens INTEGER, -- NULL = usa default de config.yaml
    timeout_seconds INTEGER, -- NULL = usa default de config.yaml
    price_input_per_million NUMERIC(12,4) NOT NULL DEFAULT 0, -- R$ por 1M tokens de entrada
    price_output_per_million NUMERIC(12,4) NOT NULL DEFAULT 0, -- R$ por 1M tokens de saída
    markup_percentage NUMERIC(6,2) NOT NULL DEFAULT 0, -- % aplicado sobre o custo acima
    status VARCHAR(10) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Tabela: plans (Planos de assinatura reutilizáveis — Teste, Mín, Pro, etc.)
CREATE TABLE IF NOT EXISTS plans (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL -- Modelo de IA usado nas respostas de clientes deste plano (NULL = usa config.yaml global, sem cobrança de crédito)
);

-- 3. Tabela: plan_area_pricing (Cota + preço por área, por plano)
CREATE TABLE IF NOT EXISTS plan_area_pricing (
    id SERIAL PRIMARY KEY,
    plan_id INTEGER REFERENCES plans(id) ON DELETE CASCADE,
    area_id INTEGER REFERENCES areas(id) ON DELETE CASCADE,
    monthly_token_quota INTEGER, -- Cota mensal de tokens (NULL = sem limite)
    price_per_1k_tokens NUMERIC(10,4), -- Taxa em R$ por 1000 tokens (NULL = custo não configurado)
    UNIQUE(plan_id, area_id)
);

-- 4. Tabela: users
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role VARCHAR(10) DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    api_key VARCHAR(64) UNIQUE, -- Chave de acesso do cliente (1 cliente = 1 chave), usada em /api/chat
    plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL, -- Plano de assinatura atual (cota/preço por área vêm daqui, vínculo ao vivo)
    balance NUMERIC(12,4) NOT NULL DEFAULT 0 -- Saldo de créditos pré-pago em R$
);

-- 5. Tabela: documents (Unificada com links e conteúdo processado)
CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,
    area_id INTEGER REFERENCES areas(id),
    name VARCHAR(255) NOT NULL, -- Nome original ou título da página
    url TEXT, -- Endereço da URL original (se for documento externo/link)
    content_text TEXT, -- Armazena o texto completo extraído do link/página ou processado do documento
    is_external_link BOOLEAN DEFAULT FALSE, -- TRUE = web; FALSE = local
    status VARCHAR(10) DEFAULT 'active' CHECK (status IN ('active', 'stale', 'invalid')),
    last_checked_at TIMESTAMPTZ, -- Data/hora da última verificação de integridade
    upload_date TIMESTAMPTZ DEFAULT NOW(), -- Quando foi inserido
    extracted_text TEXT, -- Texto extraído (uploads de arquivo), separado de content_text
    processing_status VARCHAR(20) DEFAULT 'pending' CHECK (processing_status IN ('pending', 'processing', 'indexed', 'failed')),
    chunk_count INTEGER DEFAULT 0, -- Quantos chunks o RAG gerou para este documento
    last_processed_at TIMESTAMPTZ, -- Última vez que o pipeline RAG processou este documento
    fetch_mode VARCHAR(20) DEFAULT 'http' CHECK (fetch_mode IN ('http', 'js_browser')), -- Como buscar o conteúdo de links externos
    parent_doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL -- Documento que originou este (árvore de links do Monitor Agent)
);

-- 6. Tabela: document_chunks (Mapeamento dos chunks do RAG)
CREATE TABLE IF NOT EXISTS document_chunks (
    id SERIAL PRIMARY KEY,
    doc_id INTEGER REFERENCES documents(id), -- Documento pai ao qual o chunk pertence
    area_id INTEGER, -- Facilita a busca se precisar filtrar por área
    content_chunk TEXT NOT NULL, -- O texto cortado (chunk)
    chunk_index SMALLINT NOT NULL, -- Ordem (0, 1, 2...) para reconstruir contexto depois
    embedding_vector TEXT, -- Embedding (384 dims, all-MiniLM-L6-v2) serializado como JSON
    chunk_hash TEXT, -- Hash do conteúdo do chunk, para detectar duplicatas/mudanças
    processed_at TIMESTAMPTZ DEFAULT NOW() -- Quando o embedding foi gerado
);

-- 7. Tabela: sessions (Histórico de conversas)
CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    area_id INTEGER REFERENCES areas(id),
    title TEXT, -- Título gerado ou definido pelo usuário
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 8. Tabela: messages (Mensagens com contador de tokens)
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),
    role VARCHAR(10) CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    token_count INTEGER DEFAULT 0, -- Quantos tokens essa mensagem consome
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

-- 9. Tabela: area_subscriptions (LEGADO — cota/preço por cliente+área.
-- Substituída pelos planos (plans/plan_area_pricing) acima; mantida sem
-- DROP por já ter dado histórico, mas não é mais lida nem escrita.)
CREATE TABLE IF NOT EXISTS area_subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    area_id INTEGER REFERENCES areas(id),
    status VARCHAR(10) DEFAULT 'active' CHECK (status IN ('active', 'expired')),
    expires_at TIMESTAMPTZ, -- Data de vencimento da assinatura
    monthly_token_quota INTEGER, -- Cota mensal de tokens (NULL = sem limite)
    price_per_1k_tokens NUMERIC(10,4) -- Taxa em R$ por 1000 tokens (NULL = custo não configurado)
);

-- 10. Tabela: usage_logs (Controle total de Tokens e Billing por usuário/sessão)
CREATE TABLE IF NOT EXISTS usage_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    session_id INTEGER REFERENCES sessions(id), -- Se for uso direto do sistema, pode ser null
    area_id INTEGER REFERENCES areas(id), -- Gravado direto para evitar join com sessions nos relatórios
    tokens_input INTEGER, -- Tokens da entrada (pergunta/contexto)
    tokens_output INTEGER, -- Tokens da saída (resposta gerada)
    timestamp TIMESTAMPTZ DEFAULT NOW() -- Quando ocorreu o uso
);

-- 11. Tabela: credit_transactions (Ledger do sistema de créditos pré-pago —
-- todo depósito lançado pelo admin e todo consumo de chat vira uma linha
-- aqui; users.balance é o saldo em cache, atualizado atomicamente junto)
CREATE TABLE IF NOT EXISTS credit_transactions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    type VARCHAR(12) NOT NULL CHECK (type IN ('deposit', 'consumption', 'adjustment')),
    amount NUMERIC(12,4) NOT NULL, -- Positivo = credita, negativo = debita
    balance_after NUMERIC(12,4) NOT NULL, -- Saldo após esta transação (snapshot)
    description TEXT,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    tokens_input INTEGER,
    tokens_output INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 12. Tabela: client_allowed_pages (Controle de acesso a páginas — lista
-- única e global das páginas que clientes com X-Oraculo-Key podem abrir;
-- sem chave salva no navegador = admin/uso interno, acesso total)
CREATE TABLE IF NOT EXISTS client_allowed_pages (
    page VARCHAR(100) PRIMARY KEY
);

-- ==========================================
-- Índices para performance e busca rápida
-- ==========================================
CREATE INDEX idx_documents_area ON documents(area_id);
CREATE INDEX idx_docs_url ON documents(url);
CREATE INDEX idx_document_chunks_docid ON document_chunks(doc_id);
CREATE INDEX idx_chunks_content_fts ON document_chunks USING gin (to_tsvector('portuguese', content_chunk));
CREATE INDEX idx_messages_session ON messages(session_id);
CREATE INDEX idx_usage_logs_user ON usage_logs(user_id);
CREATE INDEX idx_users_api_key ON users(api_key);
CREATE INDEX idx_usage_logs_area ON usage_logs(area_id);
CREATE INDEX idx_usage_logs_timestamp ON usage_logs(timestamp);
CREATE INDEX idx_usage_logs_user_area_time ON usage_logs(user_id, area_id, timestamp);
CREATE INDEX idx_plan_area_pricing_plan ON plan_area_pricing(plan_id);
CREATE INDEX idx_credit_transactions_user_time ON credit_transactions(user_id, created_at DESC);
