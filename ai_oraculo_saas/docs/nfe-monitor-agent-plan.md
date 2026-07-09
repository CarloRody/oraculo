# Plano: Monitor Agent API

## Objetivo

Criar uma **API independente de monitoramento** que observa recorrentemente os links externos de **todas as áreas do Tutor**, detecta atualizações nos documentos oficiais e reprocessa automaticamente no RAG. O sistema Tutor existente **não é alterado** — a nova API funciona como um serviço irmão.

---

## Problema Original

O portal `www.nfe.fazenda.gov.br/portal/listaConteudo.aspx` carrega via JavaScript dinâmico, o fetch simples do RAG não extrai nada (0 chars). Sem monitoramento recorrente os documentos ficam desatualizados quando a Receita Federal lança novas versões.

**Extensão:** O problema se aplica a **todos os 6 links externos** distribuídos entre as áreas Reformas Tributária e NFe, não só ao portal NFe.

---

## Princípios

1. **Zero alteração no Tutor** — `api/server.py` e `rag_engine.py` permanecem intactos
2. **Serviço standalone** — própria porta (`:5003`), próprio venv, próprio processo
3. **Compartilha o banco PostgreSQL** (`ai_tutor_db`) — insere novos documentos nas tabelas existentes do Tutor; cria tabelas próprias para monitoramento
4. **RAG engine como biblioteca** — importa `rag_engine.py` via sys.path, não copia

---

## Arquitetura

```
┌───────────────────────┐         ┌──────────────────────┐
│  Monitor Agent API     │────────▶│  PostgreSQL DB        │
│  FastAPI :5003         │ insere  │  ai_tutor_db          │
│                         │ docs    │  (tabelas Tutor       │
├─ URL registry           │         │   + novas tabelas)    │
├─ Fetcher (http+browser) │         └──────────────────────┘
├─ Hash comparator        │              ▲
├─ RAG pipeline wrapper   │              │ chama
├─ Change history         │              │
├─ Notifications          │              ▼
└─────────────────────────┘    ┌──────────────────────┐
                               │  Tutor (:5001, intact)│
                               │  extract.html chama   │
                               │  :5003/external       │
                               └──────────────────────┘
```

---

## Banco de Dados — Novas Tabelas

### `monitor_urls` — URLs monitoradas

```sql
CREATE TABLE monitor_urls (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255),                    -- "Portal NFe", "Gov.br Reforma"
    url TEXT UNIQUE NOT NULL,             -- URL do portal/link
    area_id INTEGER REFERENCES areas(id),-- área alvo no Tutor
    fetch_mode VARCHAR(20) DEFAULT 'http', -- 'http' ou 'js_browser'
    last_fetched_at TIMESTAMP WITH TIME ZONE,
    last_content_hash TEXT,               -- SHA256 do último conteúdo
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);
```

### `monitor_scans` — Histórico de execuções

```sql
CREATE TABLE monitor_scans (
    id SERIAL PRIMARY KEY,
    url_id INTEGER REFERENCES monitor_urls(id),
    scanned_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    content_hash TEXT,                    -- hash atual do conteúdo
    status VARCHAR(20),                   -- 'unchanged', 'changed', 'error'
    change_type VARCHAR(20),             -- 'new_doc', 'updated', 'none'
    docs_created INTEGER DEFAULT 0,       -- novos docs criados no Tutor
    docs_updated INTEGER DEFAULT 0,       -- docs atualizados no Tutor
    duration_seconds NUMERIC(6,2),
    error_message TEXT
);
```

### `monitor_extracted_links` — Links extraídos de portais JS

```sql
CREATE TABLE monitor_extracted_links (
    id SERIAL PRIMARY KEY,
    parent_url_id INTEGER REFERENCES monitor_urls(id),
    name VARCHAR(500),                    -- nome do doc/link
    url TEXT NOT NULL,
    link_type VARCHAR(20) DEFAULT 'pdf',  -- 'pdf', 'html', 'txt'
    content_hash TEXT,
    tutor_doc_id INTEGER,                 -- id em documents do Tutor (se indexado)
    last_extracted_at TIMESTAMP WITH TIME ZONE,
    UNIQUE(parent_url_id, url)
);
```

---

## Estrutura de Arquivos

```
projects/monitor-agent/
├── .venv/                    # venv separado (Python 3.10+)
├── main.py                   # FastAPI app principal (:5003)
├── scraper/
│   ├── http_fetcher.py       # Fetch simples (requests + BeautifulSoup)
│   └── js_renderer.py        # Playwright para portais JS-rendered
├── monitor/
│   ├── url_registry.py       # CRUD de URLs monitoradas
│   ├── hasher.py             # SHA256 do conteúdo, comparação
│   └── scanner.py            # Lógica principal: fetch → hash → detectar mudança
├── rag_wrapper.py            # Wrapper que importa rag_engine do Tutor
├── notifier.py               # Notificações (Telegram via OpenClaw wake)
├── config.yaml               # Configurações (portas, DB, frequência)
└── tests/
    └── test_hasher.py
```

---

## Endpoints da API (:5003)

### URLs Monitoradas
```
GET    /urls                          # Lista URLs monitoradas
POST   /urls                          # Adicionar URL para monitorar
PATCH  /urls/<id>                     # Atualizar URL
DELETE /urls/<id>                     # Remover URL
```

### Monitoramento
```
POST   /scan/all                      # Scan em todas as URLs ativas
POST   /scan/<url_id>                 # Scan de uma URL específica
GET    /scans                         # Histórico de scans (com filtros)
GET    /scans/<id>                    # Detalhe de um scan
```

### Links Extraídos (portais JS)
```
GET    /links?parent_url_id=X         # Links extraídos de um portal
POST   /links/extract                 # Forçar extração via browser
```

### RAG — Integração com Tutor
```
POST   /rag/process/<tutor_doc_id>    # Processar doc no RAG do Tutor
GET    /stats                         # Estatísticas gerais (URLs, scans, docs)
```

### Webhook para o Tutor chamar
```
POST   /api/external/extract          # Endpoint que extract.html usa
  Body: { "url": "...", "area_id": N }
  Response: { "doc_id": X, "status": "pending/indexed" }
```

### Saúde
```
GET    /health                        # Status do serviço
```

---

## Fluxo Principal de Monitoramento

```
POST /scan/all (ou cron trigger)
│
├─ Para cada URL ativa em monitor_urls:
│   │
│   ├─ Fetch conteúdo
│   │   └─ http_fetcher  (HTML/PDF estático)
│   └─ js_renderer       (portal JS-rendered, ex: nfe.fazenda.gov.br)
│
├─ Calcular SHA256 do texto extraído
│
├─ Comparar com last_content_hash no banco
│   │
│   ├─ MUDOU →
│   │   ├─ Se portal JS → extrair lista de links via browser
│   │   ├─ Para cada link novo/atualizado:
│   │   │   ├─ Fetch do PDF/texto
│   │   │   ├─ Salvar em documents (tabela Tutor)
│   │   │   └─ Chamar rag_engine.process_document() → chunk + embed
│   │   └─ Registrar scan como 'changed'
│   │
│   └─ IGUAL → registrar scan como 'unchanged'
│
├─ Salvar resultado em monitor_scans
│
└─ Se mudanças → notificar via Telegram (OpenClaw wake)
```

---

## Browser Automation — Quando Usar?

| Tipo de URL | Método | Exemplo |
|-------------|--------|---------|
| HTML estático / PDF direto | `http_fetcher` (requests + BS4) | gov.br links, PDFs diretos |
| Portal JS-rendered | `js_renderer` (Playwright headless) | nfe.fazenda.gov.br |

Configurável por URL no campo `fetch_mode`.

---

## Integração com o Tutor — extract.html

O frontend do Tutor (`extract.html`) atualmente envia para `:5001/api/documents`.
**Nova abordagem:** redirecionar apenas o extract.html para chamar a nova API:

```javascript
// Novo: envia para :5003 que cuida do fetch + RAG
await fetch('http://192.168.25.94:5003/api/external/extract', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ url, area_id })
})
```

Isso é só uma mudança de URL no frontend — **zero alteração** no backend do Tutor (server.py).

---

## Cron — Execução Automática

OpenClaw cron job semanal (**domingo 06:00 BRT**):
- Dispara `sessions_spawn` → sub-agent que chama `POST /scan/all` na API :5003
- Aguarda resultado e notifica no Telegram se houver mudanças atualizadas

---

## Tech Stack

| Componente | Escolha | Motivo |
|------------|---------|--------|
| Framework | **FastAPI** | Já tem venv FastAPI em rag-engine/; async nativo; docs auto-generados |
| Browser | **Playwright** (Python) | Headless, roda no ARM64, melhor que Puppeteer para Python |
| DB | PostgreSQL existente (`ai_tutor_db`) | Evita duplicação de infra; já configurado |
| Notificação | OpenClaw `wake` via exec | Sem depender de bot Telegram separado |

---

## Fases de Implementação

### Fase 1 — Esqueleto + DB Schema ✅ Setup Inicial
- [ ] Criar diretório `projects/monitor-agent/` e venv
- [ ] FastAPI app básica com `/health` funcional
- [ ] Executar migrações SQL (criar `monitor_urls`, `monitor_scans`, `monitor_extracted_links`)
- [ ] Implementar CRUD de URLs: `GET/POST/PATCH/DELETE /urls`
- **Estimativa:** ~1h

### Fase 2 — Fetcher HTTP + Hash
- [ ] Implementar `scraper/http_fetcher.py` (requests + BeautifulSoup)
- [ ] Implementar `monitor/hasher.py` (SHA256 do conteúdo, comparação)
- [ ] Lógica de scan básico: fetch → hash → comparar para URLs estáticas
- [ ] Endpoint `POST /scan/<url_id>` funcionando para URLs http
- **Estimativa:** ~1h

### Fase 3 — Browser JS Renderer
- [ ] Instalar Playwright + browsers no ARM64 (`pip install playwright`, `playwright install`)
- [ ] Implementar `scraper/js_renderer.py` (navegar, esperar renderização, extrair links)
- [ ] Testar com nfe.fazenda.gov.br — extrair lista de PDFs disponíveis
- [ ] Endpoint `POST /links/extract` para extração via browser
- **Estimativa:** ~2h

### Fase 4 — RAG Wrapper + Scan Completo
- [ ] Criar `rag_wrapper.py` que importa `rag_engine` do Tutor via sys.path
- [ ] Integrar scan com RAG: novo doc → salvar em documents → process_document()
- [ ] Doc atualizado → deletar chunks antigos → reprocessar
- [ ] Endpoint `POST /scan/all` funcionando completo (http + browser + RAG)
- **Estimativa:** ~1h

### Fase 5 — Extract Endpoint + Tutor Frontend
- [ ] Implementar `POST /api/external/extract` na nova API
- [ ] Atualizar `extract.html` do Tutor para apontar para :5003 em vez de :5001
- [ ] Testar fluxo completo: extract.html → :5003 → RAG → indexado ✅
- **Estimativa:** ~30min

### Fase 6 — Cron Automático + Notificação
- [ ] Configurar OpenClaw cron semanal (domingo 06:00 BRT)
- [ ] Sub-agent chama `POST /scan/all` e reporta resultado
- [ ] Implementar `notifier.py` → notificar Telegram via OpenClaw wake se houver mudanças
- **Estimativa:** ~30min

**Total estimado:** ~5h50min de trabalho distribuído em 6 fases.

---

## URLs a Monitorar (Seed Inicial)

| # | URL | Área | fetch_mode |
|---|-----|------|------------|
| 1 | `nfe.fazenda.gov.br/portal/listaConteudo.aspx` | NFe (area_id=3) | js_browser |
| 2 | `gov.br/receitafederal/.../reforma-tributaria/orientacoes-2026` | Reforma Trib. (area_id=1) | http |
| 3 | `gov.br/receitafederal/.../reforma-tributaria/entenda` | Reforma Trib. (area_id=1) | http |
| 4 | `gov.br/receitafederal/.../reforma-tributaria/marcos` | Reforma Trib. (area_id=1) | http |

---

## Considerações Técnicas

### Portal ASP.NET
- Cookies/AutoDetect: URL tem `AspxAutoDetectCookieSupport=1` — pode ser necessário manter sessão
- Links podem ter query params dinâmicos — normalizar URLs antes de comparar
- Playwright precisa esperar conteúdo JS renderizado (wait_for_selector ou wait_until 'networkidle')

### Performance ARM64
- Embeddings com `all-MiniLM-L6-v2` já funcionam, mas são lentos em docs grandes
- Monitoramento semanal = processamento batch, não é real-time → OK
- Se muitos docs atualizarem juntos, pode levar 10+ minutos

### Erros & Resiliência
- Portal indisponível → salvar erro na tabela monitor_scans, retry próxima execução
- Link quebrado → loggar aviso, continuar com outros
- PDF corrompido → extrair o que der, não travar pipeline todo

---

## Próximo Passo

Implementar **Fase 1**: criar estrutura do projeto, venv separada, FastAPI básica com `/health`, e migrações SQL das novas tabelas.
