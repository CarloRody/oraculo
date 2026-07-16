# Backup

Serviço: `backup-manager` (Flask, porta 5004). Arquivo-fonte: `backup-manager/server.py`.

## Visão geral

Backup sob demanda ou agendado de pastas do monorepo e de bancos Postgres, com alvos cadastráveis livremente pela tela (não fixos em config). Roda isolado dos outros serviços — só lê `backup_targets`, não escreve em nenhuma tabela de negócio.

## Tabela

`backup_targets` — cada linha é um alvo: `kind` (`folder`/`database`), `label`, `path` (pasta) ou `dbname` (banco), `auto_backup` (entra no agendamento automático, além do botão manual "Backup Completo"), `enabled`.

Seed inicial: os 2 projetos originais (`ai_oraculo_saas`, `oraculo_monitoragent`) + o banco principal, mais — cadastrados automaticamente em toda subida do serviço, de forma idempotente por `path` — as 3 pastas que faltavam: `backup-manager`, `whatsapp-agent`, `evolution-api`.

## Rotas

| Rota | O que faz |
|---|---|
| `GET/POST/PATCH/DELETE /api/targets` | CRUD de alvos |
| `GET /api/databases/available` | Bancos existentes no cluster, pra alimentar o cadastro |
| `GET /api/browse?path=` | Navegação read-only de pastas (restrita a `/root`), pra escolher caminho visualmente |
| `POST /api/backup/target/<id>` | Backup de um alvo específico |
| `POST /api/backup/all` | Backup de todos os alvos habilitados (ignora o flag `auto_backup`, que é só pro agendamento) |
| `GET/POST /api/schedule` | Ver/configurar agendamento automático (intervalo em horas, retenção em dias) |
| `GET/POST /api/backup/delete` | Listar / apagar arquivos de backup já gerados |

## Backup de pasta

`tar cf {arquivo} --exclude=... -C {pasta} .` — com uma lista fixa de exclusões (`FOLDER_BACKUP_EXCLUDES`): `.venv`, `venv`, `__pycache__`, `*.pyc`, `.pytest_cache`, `node_modules`, `.git`, `dist`, `build`, `*.log`. Tudo isso é regenerável via `pip install`/`npm install`/build, então não precisa estar no backup — sem essa lista, um ambiente virtual Python sozinho já inflava o backup de uma pasta pequena de código pra mais de 1GB.

## Backup de banco

`pg_dump --format=custom | gzip` — dump comprimido, timeout de 10 minutos.

## Agendamento e retenção

Thread em background checando a cada 60s se está na hora de rodar (`interval_hours` desde o último run). Quando roda automaticamente, só cobre alvos com `auto_backup=true` (diferente do botão manual, que cobre todos os habilitados) e, se `retention_days` estiver configurado, apaga backups mais antigos que isso depois de terminar. Desligado por padrão — precisa ser ativado explicitamente na tela.

## Como cadastrar uma pasta ou banco novo

Pela tela (`http://<host>:5004/`) — botão de cadastro, escolhe tipo (pasta/banco), label e caminho (com navegador de pastas embutido) ou nome do banco (lista suspensa dos bancos existentes no cluster).
