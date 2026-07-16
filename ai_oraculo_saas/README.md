> ⚠️ Este arquivo é um plano de projeto antigo, desatualizado em relação ao código atual. Para documentação técnica confiável, veja [`docs/`](../docs/README.md) na raiz do monorepo.

# AI Tutor SaaS — Project Plan

## 🎯 Objetivo

Criar um SaaS onde cada usuário pode criar chats temáticos com RAG específico por área (Engenharia, Programação, Matemática, Física, etc.). Cada área terá seu próprio índice vetorial e fundamentação teórica. O usuário cria os chats e adiciona o conteúdo do RAG.

## 📋 Estado Atual

- [X] **Fase 1 — Planejamento & Design**
  - [X] Definir stack tecnológica (frontend + backend)
  - [X] Modelo de negócio: Assinatura por área (R$ X/mês) + limite de mensagens. O usuário paga pelo acesso à área e às consultas ao RAG.
  - [ ] Mapear áreas iniciais e conteúdo RAG disponível
  - [ ] Desenhar arquitetura do sistema

- [X] **Fase 2 — Infra & Backend** ✅ CONCLUÍDA
  - [X] PostgreSQL configurado (`ai_tutor_db`) com schema completo
  - [X] API Flask rodando em `http://192.168.25.94:5001`
  - [X] Endpoints: `/api/areas`, `/api/documents` (GET+POST), `/api/stats`, `/api/search`, `/api/chat`, `/api/process/<doc_id>`
  - [X] Frontend conectado à API real (sem mais dados mockados)
  - [X] **Pipeline RAG completo** — `rag_engine.py` integrado na API:
    - Fetch de URL externa → extrai texto limpo com BeautifulSoup
    - Chunking com sentence boundary detection (~400 chars, overlap 80 chars)
    - Embeddings via `all-MiniLM-L6-v2` (384 dims, ARM64-friendly)
    - Busca semântica por cosine similarity em Python (pgvector indisponível no ARM64)
    - Texto completo salvo em `content_text` da tabela `documents` para consultas SQL diretas
  - [ ] Auth + multi-tenant setup
  - [ ] Integração com OpenClaw gateway (chat retorna contexto bruto, sem resposta do LLM ainda)

- [ ] **Fase 3 — Frontend & Portal**
  - [ ] Portal web (login, dashboard, criação de chats)
  - [ ] Interface de chat por área/RAG
  - [ ] Painel admin pra gerenciar áreas + uploads de conteúdo RAG
  - [ ] Sistema de cobrança/integração

- [ ] **Fase 4 — Testes & Lançamento**
  - [ ] Adicionar documentos nas outras 4 áreas (Engenharia, Matemática, Programação, Física)
  - [ ] Integrar OpenClaw gateway como LLM no `/api/chat`
  - [ ] Teste com áreas piloto (2-3)
  - [ ] Ajustes de performance e qualidade do RAG
  - [ ] Deploy em produção
  - [ ] Onboarding dos primeiros usuários

## 🏗️ Stack Decidida
- **Frontend & API:** Node.js (Next.js) — pendente migração
- **Motor RAG/IA:** Python (`sentence-transformers`, embeddings no Postgres como JSON text)
- **Estratégia:** Híbrida. O Node gerencia o site e usuários; o Python cuida da busca nos documentos.
> _Todas as ferramentas de ponta para vetores e embeddings são nativas do Python._

## 🔑 Notas
- Servidor atual: Orange Pi 3B / Linux ARM64
- PostgreSQL rodando, banco `ai_tutor_db` com schema aplicado e áreas seedadas (Engenharia, Matemática, Programação, Física, Reforma Tributária)
- API Flask em `http://192.168.25.94:5001` — **RAG integrado** (2026-06-16)
- OpenClaw já instalado e funcionando como LLM gateway — pendente integração no chat
- RAG por área = embeddings em `document_chunks` filtrados por `area_id`
- pgvector NÃO disponível no ARM64 → cosine similarity calculada em Python (busca todos chunks da área, ordena local)
- Modelo de embedding: `all-MiniLM-L6-v2` (384 dims, leve para ARM64)
- O usuário (Carlo) é quem cria os chats e adiciona o conteúdo RAG

## 🐛 Bugs Corrigidos (2026-06-16)
- `existing_content` não atualizada após fetch de URL externa → corrigido
- `normalize_tokens=True` (inexistente) → trocado para `normalize_embeddings=True`
- Alias SQL `dc` sem FROM correspondente → corrigido: `FROM document_chunks dc`

## 📊 Estado do Banco (2026-06-16)
- 5 áreas ativas
- 1 documento processado (gov.br reforma tributária)
- 16 chunks indexados com embeddings normalizados L2
