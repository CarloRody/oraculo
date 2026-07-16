# WhatsApp

Serviço: `whatsapp-agent` (Flask, porta 5005), deliberadamente separado do `ai_oraculo_saas` — não importa nem altera nada lá diretamente, só via HTTP. Arquivo-fonte principal: `whatsapp-agent/server.py`.

## Visão geral

Conecta números de WhatsApp reais (via QR code) a uma conta de cliente, permite resposta automática por IA seletiva por conversa, oferece uma API paga de envio de mensagens, e cobra por mensagens recebidas fora de qualquer área vinculada. A agenda de consultores (`docs/05-agenda-consultores.md`) também vive aqui.

## O conector real: Evolution API (não WAHA)

O código (`connectors/evolution.py`) e a config (`evolution_api:` em `config.yaml`) são escritos contra o contrato REST do **Evolution API** — uma plataforma WhatsApp multi-tenant em Node.js/TypeScript/Express (protocolo Baileys), vendorizada em `evolution-api/` na raiz do monorepo e rodando como `evolution-api.service` (systemd, bare-metal, **porta 8090** — confirmado em produção, é o valor real de `evolution_api.base_url`).

Existe também, em produção, um container Docker do **WAHA** (`devlikeapro/waha`, porta 8080) — outro conector WhatsApp equivalente, aparentemente testado numa época em que o Docker Hub estava limitando pulls da imagem do Evolution API. **Esse container está rodando mas não está em uso**: a config atual aponta pro Evolution API de verdade (8090), não pro WAHA (8080). Vale registrar como possível limpeza futura (parar o container ocioso), mas não é um bug — o sistema está consistente com o que está configurado.

## Tabelas envolvidas

| Tabela | Propósito |
|---|---|
| `whatsapp_accounts` | Uma conexão/número de WhatsApp. `user_id`→cliente dono, `area_id`→área vinculada (define o que a IA sabe responder ali), `ai_auto_reply_enabled` (padrão pra conversas novas) |
| `whatsapp_contacts` | Um contato conhecido numa conta |
| `whatsapp_chats` | Uma conversa (contato ou grupo). `ai_auto_reply_enabled` **por conversa** (pode divergir do padrão da conta), `booking_state` (estado da máquina de agendamento, ver doc 05) |
| `whatsapp_messages` | Mensagens individuais, indexadas por texto (busca full-text em português) |
| `whatsapp_groups`/`whatsapp_group_members`, `whatsapp_tags`/`whatsapp_contact_tags` | Organização de contatos |
| `whatsapp_settings` | Configuração por conta: horário comercial, mensagem de ausência, prompt de sistema da IA |
| `whatsapp_templates`, `whatsapp_campaigns`, `whatsapp_campaign_messages` | Estrutura pronta pra campanhas/templates (Business API), pouco usada na conexão por QR code |

A tabela `whatsapp_message_usage` (mensagens cobradas/contadas) vive no banco do **`ai_oraculo_saas`**, não aqui — é o ponto onde a cobrança de fato acontece (ver `03-clientes-planos-cobranca.md`).

## Rotas envolvidas (resumo por grupo)

| Grupo | Rotas | O que faz |
|---|---|---|
| Contas | `GET/POST /api/whatsapp/accounts`, `PATCH .../<id>`, `DELETE .../<id>`, `PUT .../area-link`, `POST .../unlink-area` | CRUD de contas; vínculo/desvínculo com área (chamado pelo `ai_oraculo_saas` ao editar cliente) |
| Conexão | `POST .../<id>/connect`, `GET .../<id>/status`, `POST .../<id>/disconnect` | Ciclo de vida QR code: gera instância no Evolution API, retorna QR, sincroniza status |
| Chats/Mensagens | `GET .../<id>/chats`, `PATCH /api/whatsapp/chats/<id>`, `GET/POST .../messages`, `POST .../chats/start`, `POST .../read` | Listar conversas, alternar IA por conversa, enviar/ler mensagens |
| Webhook | `POST /webhooks/evolution` | Entrada de tudo que chega do WhatsApp — ver fluxo abaixo |
| Consultores/Agenda | ver `docs/05-agenda-consultores.md` | |
| Docs públicas | `GET /docs` | `api-docs.html` — documentação da API paga de envio pra clientes |

## Fluxo: resposta automática por IA

1. Mensagem chega no `POST /webhooks/evolution` (evento `messages.upsert`).
2. Se a conta tem `area_id` vinculada e a conversa (`whatsapp_chats.ai_auto_reply_enabled`) está com resposta automática ligada:
3. Chama `POST /api/chat` no `ai_oraculo_saas`, autenticado com a **chave do próprio cliente** (`X-Oraculo-Key`, buscada de `users.api_key`), passando a `area_id` da conta.
4. Resposta da IA volta e é enviada de volta pro WhatsApp via `evolution.send_text()`, salva como mensagem de saída.
5. Roda em thread separada, não trava a resposta do webhook.

O toggle é **por conversa**, não só por conta — dá pra ter uma conta em "piloto automático total" e outra onde só conversas específicas respondem sozinhas (o padrão de conversas novas vem de `whatsapp_accounts.ai_auto_reply_enabled`, mas cada conversa pode ser alternada individualmente depois).

## Fluxo: mensagem sem área vinculada (cobrança de "não relacionada")

Se a conta que recebeu a mensagem não tem `area_id`, não tem IA pra responder — mas a mensagem ainda pode ser contada e cobrada: `whatsapp-agent` reporta pro `ai_oraculo_saas` via `POST /api/whatsapp/received-usage`, que decide cobrar ou não conforme `plans.charge_unrelated_received_messages` daquele cliente.

## Fluxo: API paga de envio (`POST /api/whatsapp/send`, no `ai_oraculo_saas`)

Endpoint público, pra clientes automatizarem envio de mensagens pelo próprio sistema deles (documentado em detalhe em `whatsapp-agent/public/api-docs.html`, com exemplo em PHP):
1. Cliente autentica com `X-Oraculo-Key`, informa a área.
2. `ai_oraculo_saas` confere que a área tem `plan_area_pricing.price_per_message_sent` configurado (se não tiver, bloqueia — nunca envia de graça por omissão de config) e que há saldo.
3. Resolve a conta de WhatsApp ligada àquela área (`GET /api/whatsapp/accounts` no `whatsapp-agent`) e manda enviar (`POST .../chats/start`).
4. Só debita e loga em `whatsapp_message_usage` **depois** de confirmação de envio bem-sucedido.

## Ordem de prioridade no webhook

Cada mensagem recebida passa, em ordem, por: **1)** confirmação de cadastro de consultor pendente → **2)** comando de texto "minha agenda" (reenvia link do portal) → **3)** máquina de estados de agendamento (`booking_flow.handle_incoming`, doc 05) → **4)** resposta automática por IA / cobrança de mensagem não relacionada (o que vier primeiro que "aceitar" a mensagem, para — o resto do webhook não roda em cima da mesma mensagem duas vezes).

## Frontend (`whatsapp-agent/public/`)

| Página | Rota | Uso |
|---|---|---|
| `index.html` | `/` | Painel admin: contas, conversas, agenda de consultores |
| `api-docs.html` | `/docs` | Documentação pública da API paga de envio, com exemplo em PHP |
| `consultant-portal.html` | `/agenda-consultor` | Portal do consultor — ver doc 05 |
