# Agenda de Consultores

Serviço: `whatsapp-agent` (Flask, porta 5005). Arquivos-fonte: `whatsapp-agent/booking_flow.py` (máquina de estados), `whatsapp-agent/server.py` (rotas admin e portal).

## Visão geral

Permite que um contato de WhatsApp seja promovido a "consultor", com horários de atendimento configuráveis, e que clientes finais marquem, cancelem e remarquem horário sozinhos, direto pelo WhatsApp, usando listas e botões interativos (sem app externo). O consultor tem um portal próprio (sem login, autenticado por link único) pra gerenciar a própria agenda. É um recurso de plano pago (`plans.agenda_enabled`).

## Tabelas envolvidas

| Tabela | Propósito |
|---|---|
| `whatsapp_consultants` | Contato promovido a consultor. `status` (`pending_confirmation`→`active`/`declined`/`inactive`), `weekly_availability` (JSON de horários por dia da semana), `slot_duration_minutes`, `reminder_hours_before`, `context` (texto de contexto pro agendamento), `portal_token` (credencial do portal) |
| `whatsapp_appointments` | Um agendamento: `consultant_id`, `client_contact_id`, `scheduled_at`, `duration_minutes`, `status` (`confirmed`/`cancelled`/`completed`/`no_show`), `reminder_sent_at` |
| `whatsapp_chats.booking_state` | Estado atual da máquina de agendamento daquela conversa (JSON, `null` = fora do fluxo) |

## Cadastro de um consultor

1. Admin cria o consultor a partir de um contato existente (`POST /api/whatsapp/accounts/<id>/consultants`), define disponibilidade semanal, duração de horário e antecedência do lembrete.
2. Consultor recebe uma mensagem de WhatsApp pedindo confirmação (botões Sim/Não).
3. Ao confirmar, `status` vira `active` e ele já recebe, na mesma mensagem, o link do portal próprio.
4. A qualquer momento, o consultor pode pedir o link de novo mandando **"minha agenda"**; o admin também pode forçar reenvio (e invalidar o link antigo) pelo botão "Reenviar link" no painel.

## Fluxo: cliente marca um horário pelo WhatsApp (`booking_flow.py`)

Máquina de estados por conversa, guardada em `whatsapp_chats.booking_state`:

1. **Gatilho**: cliente manda uma das palavras-chave (`agendar`, `marcar horário`, `marcar horario`) numa conta com agenda ativa e ao menos um consultor `active`. Sistema manda uma **lista** de consultores. Estado vira `choosing_consultant`.
2. **Escolha do consultor**: cliente toca numa opção da lista. Sistema calcula horários livres daquele consultor (cruzando `weekly_availability` com agendamentos já `confirmed`) e manda uma **lista** de horários. Estado vira `choosing_slot`.
3. **Escolha do horário**: cliente toca num horário. Sistema manda **botões** de confirmação (Sim/Não). Estado vira `confirming`.
4. **Confirmação**: no "Sim", cria o agendamento; no "Não" ou qualquer resposta fora do esperado, encerra o fluxo e limpa o estado.

Criação do agendamento usa `pg_advisory_xact_lock(consultant_id)` pra travar contra corrida — evita que dois clientes (ou um cliente e o próprio consultor pelo portal) reservem o mesmo horário ao mesmo tempo entre a checagem de disponibilidade e a inserção.

Funções reutilizáveis (usadas tanto pelo fluxo do cliente quanto pelo portal do consultor): `book_appointment`, `cancel_appointment_and_notify`, `reschedule_appointment_and_notify` — toda ação sobre um agendamento existente **sempre avisa o cliente por WhatsApp**, e reagendar zera `reminder_sent_at` pra o lembrete dispar de novo pro novo horário.

## Lembretes

Thread em background (`_reminder_loop`, único processo recorrente do `whatsapp-agent` — todo o resto é disparado por webhook) verifica periodicamente agendamentos que estão dentro da janela de `reminder_hours_before` e ainda não tiveam lembrete enviado, e manda uma mensagem de aviso ao cliente.

## Portal do consultor (`/agenda-consultor?token=...`)

Sem login/senha — o `portal_token` (opaco, gerado com `secrets.token_hex`) enviado por WhatsApp é a única credencial. Todas as rotas (`/api/consultant-portal/<token>/...`) resolvem o consultor estritamente pelo token, nunca confiando em id vindo do cliente sem checar dono.

O que o consultor pode fazer sozinho, pela página:
- Ver próximos horários e histórico recente.
- **Cancelar** ou **remarcar** um agendamento (remarcar mostra os horários livres de verdade, mesmo cálculo do fluxo do cliente) — cliente é avisado por WhatsApp em ambos os casos.
- **Criar** um agendamento novo pra um cliente que ligou/apareceu direto (sem passar pelo fluxo de WhatsApp).
- Editar a própria **disponibilidade semanal**, sem precisar do admin (única ação aqui que não avisa cliente nenhum, por não envolver um agendamento existente).

## Rotas — admin vs. portal

| Rotas | Quem usa |
|---|---|
| `GET/POST /api/whatsapp/accounts/<id>/consultants`, `PATCH /api/whatsapp/consultants/<id>`, `POST .../resend-portal-link`, `GET .../appointments`, `POST /api/whatsapp/appointments/<id>/cancel` | Admin, pelo painel |
| `GET /api/consultant-portal/<token>/me`, `.../appointments`, `.../free-slots`, `POST .../appointments`, `POST .../appointments/<id>/cancel`, `POST .../appointments/<id>/reschedule`, `PATCH .../availability` | Consultor, pelo portal próprio |
