# Monitoramento Automático

Serviço: `oraculo_monitoragent` (FastAPI, porta 5003). Arquivo-fonte principal: `oraculo_monitoragent/main.py`, mais o pacote `oraculo_monitoragent/scraper/`.

## Visão geral

Registra URLs externas pra serem rescaneadas periodicamente; quando o conteúdo muda, o novo texto é enviado pro `ai_oraculo_saas` pra virar (ou atualizar) um documento indexado no RAG — mantendo a base de conhecimento em dia sem trabalho manual repetido. Tem também um crawler de "árvore de conhecimento" pra indexar um site inteiro a partir de uma página raiz.

**Importante para as decisões de design deste serviço:** ele não carrega o modelo de embeddings nem faz chunking sozinho — delega essa parte pesada pro `ai_oraculo_saas` via HTTP, especificamente pra não duplicar ~1.5GB de dependências (torch/sentence-transformers) numa segunda cópia na memória de um SBC pequeno.

## Tabelas envolvidas

| Tabela | Propósito |
|---|---|
| `monitor_urls` | URL cadastrada pra monitoramento: nome, `area_id` de destino, `fetch_mode` (`http`/`js_browser`), último hash conhecido |
| `monitor_scans` | Histórico de cada rescan: status (`new`/`changed`/`unchanged`/`error`), contadores, duração |
| `monitor_extracted_links` | Links de anexo (PDF etc.) descobertos numa página monitorada |
| `monitor_crawls` / `monitor_crawl_pages` | Sessão de crawler de árvore de conhecimento a partir de uma URL raiz, e cada página visitada nela |
| `document_versions` | Fila de revisão: conteúdo novo detectado num documento que **já existe**, pendente de aprovação antes de sobrescrever |

## Rotas envolvidas

| Rota | O que faz |
|---|---|
| `GET/POST/PATCH/DELETE /urls` | CRUD de URLs monitoradas |
| `POST /scan/{url_id}` / `POST /scan/all` | Dispara rescan de uma URL ou de todas |
| `GET /scans` | Histórico de scans |
| `POST /links/extract` | Extrai links de anexo de uma página (via Playwright) |
| `POST /crawl/start` / `.../advance` / `.../finalize` / `.../cancel` | Fluxo do crawler de árvore de conhecimento |
| `POST /crawl/{id}/monitor` | Promove páginas do crawl pra monitoramento recorrente |
| `GET /versions`, `GET /versions/{id}/diff`, `POST /versions/{id}/apply`, `POST /versions/{id}/reject` | Fila de revisão de mudanças pendentes |
| `GET /dashboard` | Visão consolidada pra tela de admin (`public/index.html`) |

## Fluxo: de URL cadastrada a conteúdo indexado

1. URL é registrada (`POST /urls`), com uma área de destino e um `fetch_mode`.
2. No rescan (manual ou pelo cron interno), busca o conteúdo: `http` usa `requests`+BeautifulSoup; `js_browser` usa Playwright headless (pra portais que só renderizam com JavaScript, ex: sites em ASP.NET). Ambos também detectam e baixam anexos PDF/TXT do mesmo domínio, concatenando ao texto principal — então uma mudança só no anexo já é suficiente pra disparar detecção.
3. Calcula hash SHA-256 do texto e compara com o último conhecido: `new` (primeira vez), `changed` (hash diferente), `unchanged` (igual), `error` (falha na busca).
4. Se `new` ou `changed`:
   - **Não existe documento ainda** para essa (área, URL) → cria direto no `ai_oraculo_saas` e já dispara o processamento RAG (indexação imediata, sem revisão).
   - **Já existe documento** → não sobrescreve direto. Cria uma linha em `document_versions` com status `pending`, e qualquer versão pendente anterior daquele documento vira `superseded`. Fica esperando alguém rodar `POST /versions/{id}/apply` (ou `/reject`) pela tela.
5. O hash em `monitor_urls.last_content_hash` é atualizado assim que o scan termina, independente de a versão ter sido aprovada ou não — isso evita que o mesmo conteúdo pendente seja detectado como "mudou" de novo em todo scan seguinte, enquanto ninguém revisa.

## Fluxo: crawler de árvore de conhecimento

Pensado pra indexar um site inteiro a partir de uma página raiz, com revisão humana de quais links seguir:
1. `POST /crawl/start` — busca a página raiz, já cria o documento correspondente no `ai_oraculo_saas` e retorna os links candidatos encontrados nela.
2. `POST /crawl/{id}/advance` — você escolhe quais links seguir; cada um vira um novo documento (ligado ao pai via `parent_doc_id`), com filtro de mesmo domínio e limite de 200 páginas por sessão.
3. Repete o passo 2 quantas vezes quiser, indo mais fundo na árvore.
4. `POST /crawl/{id}/finalize` — encerra a sessão.
5. `POST /crawl/{id}/cancel` — desiste e apaga todos os documentos criados durante essa sessão (limpeza completa).
6. Opcionalmente, `POST /crawl/{id}/monitor` promove as páginas escolhidas pra `monitor_urls`, pra passarem a ser rescaneadas normalmente dali pra frente.

## Agendamento

Sem dependência externa (nem APScheduler nem cron do sistema) — um verificador de expressão cron simples embutido (`monitor/scheduler.py`), rodando em background dentro do próprio processo, checando a cada 30s se o minuto atual bate com a expressão configurada (`monitor_agent.default_cron` em `config.yaml`, padrão domingo 06:00). Quando bate, roda o mesmo `run_full_scan()` usado pelo botão manual "Scan Tudo".

## Frontend

`oraculo_monitoragent/public/index.html` — painel único: cadastro de URLs, tabela de scans recentes com status, links extraídos. Aplica a mesma checagem de página-liberada-por-cliente que as outras telas (`X-Oraculo-Key` contra `GET /api/allowed-pages` no `ai_oraculo_saas`).
