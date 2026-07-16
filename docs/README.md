# Oráculo — Documentação Técnica

Documentação de referência do sistema completo: arquitetura, banco de dados, rotas HTTP e fluxos de informação de cada funcionalidade. Escrita para consulta rápida — sua e de qualquer sessão futura de IA trabalhando neste repositório.

> Esta é a documentação **atual e confiável**. Os `README.md`/`db.md` dentro de cada serviço podem estar desatualizados (veja a nota em cada um) — em caso de divergência, confie nestes arquivos aqui.

## O que é o Oráculo

Uma plataforma de assistente de IA com base de conhecimento própria por cliente ("área"), organizada em torno de RAG (Retrieval-Augmented Generation): você indexa documentos e links, e a IA responde só com base neles, com citação de fonte. Além do chat próprio, o mesmo conhecimento pode responder direto no WhatsApp da empresa, incluindo agendamento de horários por conversa.

Cobrança é toda por saldo pré-pago em R$, debitado por consumo (tokens de IA, mensagens de WhatsApp) — sem mensalidade fixa surpresa. Não existe cadastro nem pagamento self-service hoje: tudo é feito manualmente pelo admin (veja `docs/03-clientes-planos-cobranca.md`).

## Arquitetura

4 serviços Python independentes + 1 conector externo vendorizado, todos no mesmo host (Orange Pi 3B, ARM64), compartilhando **um único banco Postgres** (`ai_tutor_db`) — cada serviço só *escreve* nas suas próprias tabelas, mas pode *ler* tabelas de outro serviço diretamente quando isso evita uma chamada HTTP desnecessária (padrão usado poucas vezes, sempre documentado onde acontece).

| Serviço | Framework | Porta | Responsabilidade |
|---|---|---|---|
| `ai_oraculo_saas` | Flask | 5001 | Serviço principal: áreas, documentos, RAG, chat, clientes, planos, cobrança, painel admin, páginas públicas |
| `whatsapp-agent` | Flask | 5005 | Integração com WhatsApp: contas, contatos, chats, resposta automática por IA, API paga de envio, agenda de consultores |
| `oraculo_monitoragent` | FastAPI | 5003 | Monitoramento de URLs externas: rescan periódico, detecção de mudança, alimenta o RAG do `ai_oraculo_saas` |
| `backup-manager` | Flask | 5004 | Backup sob demanda/agendado de pastas do monorepo e do banco Postgres |
| `evolution-api` (externo, vendorizado) | Node.js/Express | 8090 | Conector WhatsApp real (protocolo Baileys) — ver `docs/04-whatsapp.md` para a confusão de nomenclatura com um container WAHA não usado |

Cada serviço roda como um `systemd` service próprio (`ai-tutor-api`, `whatsapp-agent`, `monitor-agent`, `backup-manager`, `evolution-api`) no host de produção.

### Comunicação entre serviços

- **HTTP entre processos** é a regra: cada serviço expõe uma API HTTP e chama a do outro quando precisa de algo que não é seu. Exemplos: `whatsapp-agent` chama `POST /api/chat` em `ai_oraculo_saas` pra gerar a resposta de IA; `ai_oraculo_saas` chama `whatsapp-agent` pra efetivamente enviar uma mensagem.
- **Leitura direta de tabela alheia** acontece em pontos pontuais e documentados, sempre justificada por evitar duplicar uma chamada HTTP trivial dentro do mesmo host — ex: `whatsapp-agent` lê `plans.agenda_enabled` direto do Postgres em vez de perguntar pro `ai_oraculo_saas`.
- Chamadas cruzadas costumam ser **fail-soft**: se o outro serviço estiver fora do ar, quem chama loga o erro e segue (não derruba o próprio serviço por causa do outro).

### Banco de dados

Um Postgres só (`ai_tutor_db`), sem `pgvector` (indisponível no ARM64 do host) — busca semântica é feita calculando similaridade de cosseno em Python puro sobre embeddings guardados como JSON (ver `docs/01-conhecimento-e-chat.md`). Cada serviço tem seu próprio módulo de migrações idempotentes (`schema.sql`/`migrations.py` em `ai_oraculo_saas`, `db_migrations.py` nos outros 3), todas aplicadas automaticamente no início do processo (`CREATE TABLE IF NOT EXISTS`, nunca destrutivas).

### Deploy

Fluxo único pros 4 serviços: editar localmente → `git commit`/`push` pra `origin/main` → no servidor, `git pull` → migração roda sozinha no próximo start → `systemctl restart <serviço>`. Páginas HTML estáticas (as de `frontend/` e `public/`) não precisam de restart — são lidas do disco a cada request.

## Índice dos documentos

| Arquivo | Cobre |
|---|---|
| [`01-conhecimento-e-chat.md`](01-conhecimento-e-chat.md) | Áreas, documentos, pipeline RAG, chat, Pesquisa 3 PRO High |
| [`02-monitoramento-automatico.md`](02-monitoramento-automatico.md) | `oraculo_monitoragent`: scan de URLs, diffs, versões pendentes, crawler de árvore de conhecimento |
| [`03-clientes-planos-cobranca.md`](03-clientes-planos-cobranca.md) | Clientes, chave de acesso, planos, modelos de IA, saldo/créditos, painel admin |
| [`04-whatsapp.md`](04-whatsapp.md) | Contas, contatos, chats, resposta automática, webhook, API paga de envio, o conector Evolution API |
| [`05-agenda-consultores.md`](05-agenda-consultores.md) | Consultores, agendamentos, máquina de estados de reserva, portal do consultor |
| [`06-backup.md`](06-backup.md) | `backup-manager`: alvos, exclusões, agendamento/retenção |
| [`07-paginas-publicas.md`](07-paginas-publicas.md) | `vendas.html`, `ajuda.html` — vitrine e central de ajuda públicas |
