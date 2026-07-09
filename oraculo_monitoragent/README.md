# Monitor Agent — Dashboard de Monitoramento Externo

Serviço independente que monitora URLs externas e integra mudanças ao RAG do AI Tutor SaaS. Zero alterações no Tutor.

## Arquitetura

```
Monitor Agent (FastAPI :5003) ──┬── PostgreSQL ai_tutor_db
                                 │    ├── monitor_urls
                                 │    ├── monitor_scans
                                 │    └── monitor_extracted_links
                                 │
                                 ├── Tutor API (HTTP :5001, POST /api/process/<doc_id>)
                                 │     └── chunking + embeddings → document_chunks
                                 │
                                 └── Dashboard Frontend (/public/index.html)
```

## Stack

| Camada | Tecnologia |
|--------|-----------|
| API | FastAPI + uvicorn (porta 5003) |
| Banco | PostgreSQL `ai_tutor_db` (mesmo DB do Tutor, tabelas isoladas) |
| RAG | Delega para a API do Tutor (`POST :5001/api/process/<doc_id>`) via `rag_wrapper.py` — evita duplicar sentence-transformers/torch e carregar um 2º modelo em RAM |
| Scraper | Playwright (JS-rendered portals) + httpx (páginas estáticas) |
| Frontend | HTML/CSS/JS vanilla — dark theme, auto-refresh 30s |
| Deploy | systemd service `monitor-agent.service` |

## API Endpoints

### Health
- `GET /health` — status do serviço e conexão DB
- `GET /health/extended` — inclui verificação RAG engine
- `GET /stats` — URLs registradas + scans realizados

### URLs CRUD
- `GET /urls?enabled_only=true` — lista URLs monitoradas
- `POST /urls` — cria URL (`name`, `url`, `area_id`, `fetch_mode`, `enabled`)
- `PATCH /urls/{id}` — atualiza campos parciais
- `DELETE /urls/{id}` — remove URL

### Scans
- `POST /scan/all` — scan de todas URLs enabled (persiste + RAG)
- `POST /scan/{url_id}` — scan individual (persiste + RAG)
- `GET /scans?limit=50&url_id=X` — histórico de scans

### Links Extraídos
- `GET /links?parent_url_id=X` — lista links extraídos
- `POST /links/extract?url_id=X` ou `?url=X` — extrai links via Playwright

### Dashboard
- `GET /dashboard` — endpoint all-in-one (URLs + scans + links + tutor stats + areas)
- `GET /` — serve o frontend HTML
- `/static/*` — arquivos estáticos do frontend

## Frontend

**Dashboard:** `http://192.168.25.94:5003/static/index.html` (ou `/`)

Seções:
1. **Top bar** — título + indicador de saúde da API + último refresh
2. **Stats cards** — URLs registradas, scans realizados, docs/chunks Tutor RAG
3. **Formulário** — adicionar nova URL (nome, URL, área, fetch mode)
4. **Tabela URLs** — lista com ações (scan individual, toggle enabled, delete)
5. **Scans recentes** — últimos 20 scans com badges de status coloridos
6. **Links extraídos** — expandível por portal pai

Design: tema dark (#1a19e primary, #16213e secondary), auto-refresh 30s.

## Banco de Dados

Tabelas criadas no DB `ai_tutor_db` via migrations (`db_migrations.py`):

```sql
-- URLs monitoradas
CREATE TABLE monitor_urls (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    url TEXT UNIQUE NOT NULL,
    area_id INTEGER REFERENCES areas(id),
    fetch_mode VARCHAR(20) DEFAULT 'http',  -- 'http' | 'js_browser'
    enabled BOOLEAN DEFAULT true,
    last_content_hash TEXT,
    last_fetched_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Histórico de scans
CREATE TABLE monitor_scans (
    id SERIAL PRIMARY KEY,
    url_id INTEGER REFERENCES monitor_urls(id),
    content_hash TEXT,
    status VARCHAR(20),           -- 'unchanged' | 'changed' | 'new' | 'error'
    change_type VARCHAR(50),      -- 'created' | 'updated' | 'deleted'
    docs_created INTEGER DEFAULT 0,
    docs_updated INTEGER DEFAULT 0,
    duration_seconds NUMERIC,
    error_message TEXT,
    scanned_at TIMESTAMPTZ DEFAULT NOW()
);

-- Links extraídos de portals JS-rendered
CREATE TABLE monitor_extracted_links (
    id SERIAL PRIMARY KEY,
    parent_url_id INTEGER REFERENCES monitor_urls(id),
    name VARCHAR(255) NOT NULL,
    url TEXT NOT NULL,
    link_type VARCHAR(50),        -- 'pdf' | 'doc' | 'xml' etc.
    last_extracted_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(parent_url_id, url)
);
```

## Deploy

- **Service:** `monitor-agent.service` (systemd, auto-restart, enabled em boot)
- **Working dir:** `/root/.openclaw/workspace/projects/monitor-agent`
- **Venv:** `.venv/` com FastAPI, uvicorn, psycopg2, httpx, playwright, yaml

## Roadmap / Fases

### ✅ Fase 1 — Infraestrutura (Jun 29)
- [x] DB schema + migrations
- [x] Scanner básico (hash SHA-256 de conteúdo)
- [x] URL registry (CRUD PostgreSQL)
- [x] FastAPI API endpoints

### ✅ Fase 2 — Scans + RAG Integration (Jul 01)
- [x] Pipeline de scan → persiste no DB
- [x] RAG integration via `rag_wrapper.py` (chama a API HTTP do Tutor, `POST /api/process/<doc_id>`)
- [x] On change: cria/atualiza doc no Tutor + chunking + embeddings

### ✅ Fase 3 — Dashboard Frontend (Jul 02)
- [x] HTML/CSS/JS vanilla com tema dark
- [x] Endpoint `/dashboard` all-in-one
- [x] CRUD de URLs via UI
- [x] Tabela de scans recentes com status badges
- [x] Auto-refresh 30s + indicador de saúde da API
- [x] Serving estático no FastAPI (`/static/` + `/`)

### ✅ Fase 4 — Systemd Service (Jul 02)
- [x] `monitor-agent.service` com auto-restart
- [x] Enabled em boot, depende do PostgreSQL

### 🔄 Fase 5 — Scraper JS + Link Extraction (pending)
- [ ] Playwright scraper para portals renderizados via JS
- [ ] Extração de links PDF/DOC/XML de páginas dinâmicas
- [ ] Salvar links extraídos no DB (`monitor_extracted_links`)
- [ ] Endpoint `/links/extract` com Playwright headless

### 🔄 Fase 6 — Cron Scheduling (pending)
- [ ] Scheduler interno para scans periódicos (config.yaml `default_cron`)
- [ ] Suporte a cron expression por URL individual
- [ ] Notificações de mudança via webhook/chat

## Configuração

`config.yaml`:
```yaml
server: { host: "0.0.0.0", port: 5003 }
database: { dbname: "ai_tutor_db", user: "postgres", host: "/var/run/postgresql" }
monitoring: { default_cron: "0 6 * * 0", fetch_timeout: 30 }
```

Variável de ambiente `TUTOR_API_URL` (opcional, default `http://localhost:5001`): base URL da API do Tutor usada por `rag_wrapper.py` para processar documentos via HTTP.

## Acesso Remoto (VPN)

**Recomendado:** Tailscale — rede mesh sem abrir portas, NAT traversal automático.
Alternativas: WireGuard (porta UDP 51820 no router), ZeroTier (control plane na nuvem).
