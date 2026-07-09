-- AI Tutor SaaS - Estrutura do Banco de Dados (PostgreSQL)

-- 1. Tabela: users
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role VARCHAR(10) DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Tabela: areas (Engenharia, Matemática, etc.)
CREATE TABLE IF NOT EXISTS areas (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    slug VARCHAR(50) UNIQUE NOT NULL,
    vector_ref TEXT NOT NULL, -- Nome do índice no ChromaDB/Qdrant (ex: 'area_engenharia_v1')
    status VARCHAR(10) DEFAULT 'draft' CHECK (status IN ('active', 'draft', 'archived'))
);

-- 3. Tabela: documents (Unificada com links e conteúdo processado)
CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,
    area_id INTEGER REFERENCES areas(id),
    name VARCHAR(255) NOT NULL, -- Nome original ou título da página
    url TEXT, -- Endereço da URL original (se for documento externo/link)
    content_text TEXT, -- Armazena o texto completo extraído do link/página ou processado do documento
    is_external_link BOOLEAN DEFAULT FALSE, -- TRUE = web; FALSE = local
    status VARCHAR(10) DEFAULT 'active' CHECK (status IN ('active', 'stale', 'invalid')),
    last_checked_at TIMESTAMPTZ, -- Data/hora da última verificação de integridade
    upload_date TIMESTAMPTZ DEFAULT NOW() -- Quando foi inserido
);

-- 4. Tabela: document_chunks (Mapeamento dos chunks do RAG)
CREATE TABLE IF NOT EXISTS document_chunks (
    id SERIAL PRIMARY KEY,
    doc_id INTEGER REFERENCES documents(id), -- Documento pai ao qual o chunk pertence
    area_id INTEGER, -- Facilita a busca se precisar filtrar por área
    content_chunk TEXT NOT NULL, -- O texto cortado (chunk)
    chunk_index SMALLINT NOT NULL -- Ordem (0, 1, 2...) para reconstruir contexto depois
);

-- 5. Tabela: sessions (Histórico de conversas)
CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    area_id INTEGER REFERENCES areas(id),
    title TEXT, -- Título gerado ou definido pelo usuário
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 6. Tabela: messages (Mensagens com contador de tokens)
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),
    role VARCHAR(10) CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    token_count INTEGER DEFAULT 0, -- Quantos tokens essa mensagem consome
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

-- 7. Tabela: area_subscriptions (Controle de acesso e Billing)
CREATE TABLE IF NOT EXISTS area_subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    area_id INTEGER REFERENCES areas(id),
    status VARCHAR(10) DEFAULT 'active' CHECK (status IN ('active', 'expired')),
    expires_at TIMESTAMPTZ -- Data de vencimento da assinatura
);

-- 8. Tabela: usage_logs (Controle total de Tokens e Billing por usuário/sessão)
CREATE TABLE IF NOT EXISTS usage_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    session_id INTEGER REFERENCES sessions(id), -- Se for uso direto do sistema, pode ser null
    tokens_input INTEGER, -- Tokens da entrada (pergunta/contexto)
    tokens_output INTEGER, -- Tokens da saída (resposta gerada)
    timestamp TIMESTAMPTZ DEFAULT NOW() -- Quando ocorreu o uso
);

-- ==========================================
-- Índices para performance e busca rápida
-- ==========================================
CREATE INDEX idx_documents_area ON documents(area_id);
CREATE INDEX idx_document_chunks_docid ON document_chunks(doc_id);
CREATE INDEX idx_messages_session ON messages(session_id);
CREATE INDEX idx_usage_logs_user ON usage_logs(user_id);
