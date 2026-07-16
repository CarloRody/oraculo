# Conhecimento e Chat

Serviço: `ai_oraculo_saas` (Flask, porta 5001). Arquivos-fonte principais: `ai_oraculo_saas/api/server.py`, `ai_oraculo_saas/rag_engine.py`.

## Visão geral

Cada "área" é uma base de conhecimento temática. Documentos (arquivos ou links) são indexados nela via um pipeline de RAG (chunking + embeddings). O chat responde perguntas buscando os trechos mais relevantes entre as áreas autorizadas do cliente e citando as fontes usadas. Existe também um modo de pesquisa mais pesado ("Pesquisa 3 PRO High") que combina RAG com busca na web e reconciliação entre 3 agentes.

## Tabelas envolvidas

| Tabela | Propósito | Colunas-chave |
|---|---|---|
| `areas` | Base de conhecimento temática | `owner_user_id` nulo = área global (compartilhada); preenchido = área privada de um cliente. `custom_prompt` é injetado no chat, na pesquisa e no bot de WhatsApp dessa área. `status` (`active`/`draft`/`archived`). |
| `documents` | Um arquivo ou link, com seu texto extraído | `area_id`, `url` (nulo se for upload de arquivo), `fetch_mode` (`http` ou `js_browser` via Playwright, pra portais que só renderizam com JS), `content_text`, `processing_status` (`pending`→`processing`→`indexed`/`failed`), `chunk_count`, `parent_doc_id` (monta árvore de links, usada pelo monitor) |
| `document_chunks` | Pedaço de texto de um documento + seu embedding | `content_chunk`, `chunk_index`, `embedding_vector` (JSON de 384 números, sem pgvector), `chunk_hash` (dedup) |

## Rotas envolvidas

| Rota | O que faz |
|---|---|
| `GET /api/areas` | Áreas que a chave do cliente autoriza |
| `GET /api/my-area` | A área privada do próprio cliente (`owner_user_id` = ele) |
| `GET/POST /api/documents` | Lista / cria documento (POST já dispara o processamento RAG) |
| `POST /api/process/<doc_id>` | Reprocessa um documento específico |
| `POST /api/upload` | Upload de PDF/TXT (multipart), extrai texto e processa |
| `POST /api/search` | Busca semântica crua (usada por `rag.html`) |
| `POST /api/chat` | Endpoint principal do chat — ver fluxo abaixo |
| `POST /api/agent-research` | Pesquisa 3 PRO High — ver fluxo abaixo |
| `GET /api/stats` | Contagem de áreas/documentos/chunks ativos + status do modelo RAG carregado |
| `GET/PATCH/DELETE /admin/areas`, `/admin/documents/*` | CRUD administrativo (criar área, editar/apagar documento, reprocessar, detectar duplicatas e documentos de baixa qualidade) |

## Fluxo: indexação de um documento

1. Documento é criado via `POST /api/documents` (link, geralmente vindo do monitor ou de `meu-portal.html`/`extract.html`) ou `POST /api/upload` (arquivo).
2. Se for link sem texto em cache, busca o conteúdo: `requests`+BeautifulSoup (modo `http`) ou Playwright headless (modo `js_browser`, pra sites que dependem de JavaScript).
3. Texto vai pra `documents.content_text`.
4. `chunk_text()` quebra em pedaços de ~400 caracteres respeitando fim de frase, com 80 caracteres de sobreposição entre pedaços vizinhos.
5. Cada pedaço vira um embedding via `sentence-transformers` (`all-MiniLM-L6-v2`, 384 dimensões — escolhido por ser leve o suficiente pra rodar num ARM64 pequeno).
6. Chunks (com embedding) são salvos em `document_chunks`; `documents.processing_status` vira `indexed` (ou `failed` se algo quebrar).

## Fluxo: uma pergunta no chat (`POST /api/chat`)

1. Resolve o cliente pela chave (`X-Oraculo-Key`), confere que a conta está ativa e tem saldo.
2. Resolve quais áreas ele pode consultar (todas as autorizadas, ou um subconjunto escolhido na tela).
3. `search_similar()` calcula a similaridade de cosseno (em Python puro — sem índice ANN, sem pgvector, porque não está disponível no ARM64 do host) entre o embedding da pergunta e **todos** os chunks das áreas pedidas. Se for mais de uma área, reserva uma fatia de resultados por área (`top_k // nº de áreas`) pra uma área com scores mais altos não engolir o resultado de outra.
4. Monta o prompt com os melhores trechos + o `custom_prompt` de cada área envolvida, chama o modelo de IA configurado no plano do cliente (ou a configuração global, se o plano não tiver modelo).
5. Resposta volta com as fontes usadas (documento + trecho), pra exibição com citação.
6. Loga o consumo em `usage_logs` e debita o saldo em `credit_transactions` (detalhe da cobrança em `03-clientes-planos-cobranca.md`).

## Fluxo: Pesquisa 3 PRO High (`POST /api/agent-research`)

Mesmas checagens de autenticação/saldo/área do `/api/chat`, mas com 3 agentes em vez de 1:
1. **Agente 1** responde só com base no RAG/documentos indexados.
2. **Agente 2** pesquisa a mesma pergunta na internet (via SearXNG local).
3. **Agente 3** reconcilia as duas respostas, tratando a documentação oficial (agente 1) como fonte de verdade quando há conflito.

Cobrança tem um multiplicador extra configurável por modelo de IA (`ai_models.pro_high_multiplier`), porque usa mais tokens que uma pergunta simples.

## Páginas envolvidas (`ai_oraculo_saas/frontend/`)

| Página | Uso |
|---|---|
| `chat.html` | Chat do cliente — escolhe área(s), pergunta, vê fontes e saldo |
| `meu-portal.html` | Cliente sobe seus próprios documentos/links pra área privada dele, vê saldo e histórico |
| `rag.html` | Ferramenta de teste de busca semântica crua (`/api/search`), uso interno/admin |
| `pesquisa-agentes.html` | UI da Pesquisa 3 PRO High |
| `extract.html` | Ferramenta admin de extração/cadastro manual de documento |
| `tree.html` | Visualização em árvore de documentos ligados por `parent_doc_id` (links descobertos pelo monitor) |
