# Clientes, Planos e Cobrança

Serviço: `ai_oraculo_saas` (Flask, porta 5001). Arquivo-fonte: `ai_oraculo_saas/api/server.py`, painel em `ai_oraculo_saas/frontend/admin.html`.

## Visão geral

Não existe cadastro nem login self-service. Todo cliente é criado manualmente pelo admin, recebe uma chave de acesso única, e opera num modelo de saldo pré-pago em R$ debitado por consumo (tokens de IA e mensagens de WhatsApp). Planos definem cotas e preços por área; modelos de IA definem qual backend real responde e quanto custa.

## Tabelas envolvidas

| Tabela | Propósito |
|---|---|
| `users` | Identidade do cliente. `api_key` é a única credencial (header `X-Oraculo-Key`). `balance` é o saldo em R$ (cache, mantido em sincronia com `credit_transactions`). `access_restricted`/`status` controlam acesso a páginas e à conta inteira. |
| `plans` | Modelo reutilizável de assinatura: nome, `model_id` (qual `ai_models` usar), flags de cobrança de mensagens WhatsApp fora de área, `agenda_enabled` (libera a agenda de consultores) |
| `plan_area_pricing` | Preço por (plano, área): cota mensal de tokens, R$/1k tokens, R$/mensagem WhatsApp enviada |
| `ai_models` | Backend de IA real (endpoint + chave), com preço por 1M de tokens de entrada/saída + markup%, e um multiplicador extra pra Pesquisa 3 PRO High |
| `credit_transactions` | Livro-razão do saldo — toda cobrança (`consumption`), todo depósito (`deposit`) e todo ajuste manual (`adjustment`) vira uma linha aqui; `users.balance` é o total corrente |
| `usage_logs` | Registro de consumo de tokens por sessão/área, fonte dos relatórios de uso |
| `client_allowed_pages` | Lista branca de páginas por cliente (só vale quando `users.access_restricted = true`) |

## Como um cliente é criado e acessa o sistema

1. Admin cria o cliente (`POST /admin/users`) — gera uma `api_key` única.
2. Admin associa um plano (`plans.id`) e, se o cliente precisar de conhecimento próprio, cria uma área privada pra ele (`owner_user_id` = o cliente).
3. Cliente recebe a chave (hoje, entregue manualmente — por WhatsApp, por exemplo).
4. Login = colar a chave: `index.html` tem um botão "Entrar" que pede a chave via `prompt()` e guarda em `localStorage['oraculo_api_key']`. Todo request subsequente do navegador manda essa chave no header `X-Oraculo-Key`.
5. `access-guard.js` (incluído em toda página exceto as públicas) confere, a cada carregamento de página, se aquele cliente tem acesso restrito e a quais páginas — falha aberta (erro de rede não bloqueia).

## Como o saldo é debitado

- Cada resposta de chat/pesquisa: `usage_logs` recebe uma linha com os tokens consumidos; o preço (`plan_area_pricing.price_per_1k_tokens` da área usada) determina quanto sai do saldo; `credit_transactions` registra a saída e `users.balance` é atualizado atomicamente.
- Cada mensagem WhatsApp enviada pela API paga, ou recebida fora de área (se o plano cobrar isso): mesma lógica, via `plan_area_pricing.price_per_message_sent` ou `plans.price_per_unrelated_message` — detalhado em `04-whatsapp.md`.
- Recarga: hoje só manual, `POST /admin/credits/deposit` — não existe gateway de pagamento integrado (proposital, ver `07-paginas-publicas.md`).
- Se o saldo chegar a zero, `/api/chat` e `/api/agent-research` recusam a requisição (e a resposta automática de WhatsApp para) até o saldo ser recarregado.

## Painel admin (`admin.html`) — abas relevantes

| Aba | Rotas por trás | O que permite |
|---|---|---|
| Clientes | `/admin/users/*`, `/admin/credits/*` | Criar/editar cliente, ver/ajustar saldo, ver uso de WhatsApp do mês, restringir páginas |
| Planos | `/admin/plans/*` | Criar/editar plano, tabela de preço por área, toggles de cobrança de WhatsApp e agenda. Tem botão **Duplicar** — reaproveita o modal de edição só trocando o id pra vazio, útil porque um plano tem muitos campos |
| Modelos | `/admin/models/*` | Cadastro de backends de IA reais (endpoint, chave, preço, markup). Também tem **Duplicar** |
| Áreas | `/admin/areas/*` | Criar/editar/arquivar área |
| Documentos | `/admin/documents/*` | Ver/editar/reprocessar documentos, detectar duplicatas e conteúdo de baixa qualidade |
| Relatórios | `/admin/usage-summary`, `/admin/usage-report` | Consumo por período/área/cliente com custo estimado |
| Configurações | `/admin/config`, `/admin/ddns` | Config de LLM/banco/DDNS, restart dos serviços |

## Páginas do cliente

- `chat.html` — conversa com a IA (ver `01-conhecimento-e-chat.md`)
- `meu-portal.html` — saldo, histórico de créditos, upload de documentos/links pra sua própria área
