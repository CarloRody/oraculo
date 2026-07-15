# 🗄️ Estrutura do Banco de Dados — AI Tutor SaaS (Final e Unificada)

## Visão Geral

O sistema centraliza quase tudo no **PostgreSQL**. A tabela `documents` unifica tanto documentos físicos quanto links externos, contendo um campo grande para armazenar todo o texto/link.

---

## 1. Tabelas SQL — PostgreSQL

### `users`
| Campo | Tipo | Descrição |
| --- | --- | --- |
| id | SERIAL PK | ID único |
| email | VARCHAR(255) UNIQUE | Login principal |
| password_hash | TEXT | Hash da senha |
| role | VARCHAR(10) DEFAULT 'user' | `admin` (gerencia RAG), `user` (consulta) |

### `areas`
| Campo | Tipo | Descrição |
| --- | --- | --- |
| id | SERIAL PK | ID da área |
| name | VARCHAR(100) | Nome (ex: Engenharia, Matemática) |
| slug | VARCHAR(50) UNIQUE | URL friendly (`engenharia`) |
| vector_ref | TEXT | Nome do índice no Vector DB (ex: `area_engenharia_v1`) |
| status | VARCHAR(10) DEFAULT 'draft' | `active`, `draft` ou `archived` |
| owner_user_id | INTEGER FK → users.id | NULL = área global; preenchida = base de conhecimento privada de um cliente |
| custom_prompt | TEXT | Instruções extras da área, injetadas no prompt de `/api/chat`, `/api/agent-research` e no bot de WhatsApp |

### `documents` — Tabela Unificada de Documentos e Links Externos
Tabela única que armazena tanto documentos físicos (PDFs/DOCX) quanto links externos (URLs). Contém um campo grande para armazenar todo o texto/link completo.
| Campo | Tipo | Descrição |
| --- | --- | --- |
| id | SERIAL PK | ID do documento |
| area_id | INTEGER FK → areas.id | Área de origem |
| is_external_link | BOOLEAN DEFAULT FALSE | `TRUE` = página web externa; `FALSE` = arquivo local (PDF/DOCX) |
| name | VARCHAR(255) | Nome original ou título da página |
| url | TEXT | Endereço da URL original (se for documento externo/link) |
| content_text | TEXT LARGE | **Campo grande** para armazenar o texto completo extraído do link/página ou o conteúdo processado do documento |
| status | VARCHAR(10) DEFAULT 'active' | `active` (ok), `stale` (desatualizado/roupa), `invalid` (erro 404/etc.) |
| last_checked_at | TIMESTAMPTZ | Data/hora da última verificação de integridade |
| upload_date | TIMESTAMPTZ DEFAULT NOW() | Quando foi inserido |

### `sessions`
| Campo | Tipo | Descrição |
| --- | --- | --- |
| id | SERIAL PK | ID da sessão |
| user_id | INTEGER FK → users.id | Dono da conversa |
| area_id | INTEGER FK → areas.id | Área consultada |
| title | TEXT | Título gerado ou definido pelo usuário |
| created_at | TIMESTAMPTZ DEFAULT NOW() | Data de criação |

### `messages` — Com Contador de Tokens
| Campo | Tipo | Descrição |
| --- | --- | --- |
| id | SERIAL PK | ID da mensagem |
| session_id | INTEGER FK → sessions.id | Sessão a que pertence |
| role | VARCHAR(10) | `user` ou `assistant` |
| content | TEXT | Mensagem |
| token_count | INTEGER DEFAULT 0 | Quantos tokens essa mensagem consome (estimativa ou real) |

### `area_subscriptions` — Controle de Acesso e Billing
| Campo | Tipo | Descrição |
| --- | --- | --- |
| id | SERIAL PK | ID da assinatura |
| user_id | INTEGER FK → users.id | Usuário assinante |
| area_id | INTEGER FK → areas.id | Área contratada |
| status | VARCHAR(10) DEFAULT 'active' | `active` ou `expired` |
| expires_at | TIMESTAMPTZ | Data de vencimento |

### `usage_logs` — Controle de Tokens e Billing (Contador de Tokens)
Tabela principal para saber quanto cada usuário consome em tokens.
| Campo | Tipo | Descrição |
| --- | --- | --- |
| id | SERIAL PK | ID do registro |
| user_id | INTEGER FK → users.id | Usuário consumidor |
| session_id | INTEGER FK → sessions.id | Sessão associada (se houver) |
| tokens_input | INTEGER | Tokens da entrada (pergunta/contexto) |
| tokens_output | INTEGER | Tokens da saída (resposta gerada) |
| timestamp | TIMESTAMPTZ DEFAULT NOW() | Quando ocorreu o uso |

### `whatsapp_message_usage` — Medição de mensagens WhatsApp cobradas
Mensagens enviadas via `/api/whatsapp/send` (API pública do cliente) ou recebidas numa conexão sem área vinculada.
| Campo | Tipo | Descrição |
| --- | --- | --- |
| id | SERIAL PK | ID do registro |
| user_id | INTEGER FK → users.id | Cliente |
| area_id | INTEGER FK → areas.id | Área usada no envio (NULL = mensagem recebida sem área) |
| direction | VARCHAR(10) | `sent` ou `received` |
| price_charged | NUMERIC(10,4) | Valor debitado do saldo (NULL = contada mas não cobrada) |
| wa_account_id | INTEGER | id da conta no whatsapp-agent (sem FK, serviço separado) |
| created_at | TIMESTAMPTZ DEFAULT NOW() | Quando ocorreu |

---

## 2. Vector DB — ChromaDB (ou Qdrant)

O banco relacional atua como a fonte da verdade para metadados e controle de acesso. Os embeddings são gerados com base nos dados binários (`content_text`) da tabela `documents`.

### Pipeline de ingestão (quando o admin adiciona um documento ou link externo)
```python
# 1. Ler conteúdo texto do Postgres (seja PDF processado ou página web extraída)
doc = session.query(Document).get(doc_id)
content_text = doc.content_text.read() 

# 2. Dividir em chunks de ~500 tokens
chunks = TextSplitter(chunk_size=500, chunk_overlap=50).split_documents(content_text)

# 3. Gerar embeddings e salvar no índice da área específica
collection.add(
    documents=[chunk.page_content for chunk in chunks],
    metadatas=[{"doc_id": doc.id, "area": area_name} for chunk in chunks]
)
```

### Consulta com verificação de link dinâmico (RAG + Link Status)
```python
# Recuperar contexto técnico específico da área
collection = client.get_or_create_collection(name="area_engenharia_v1")
results = collection.query(
    query_texts=["pergunta do usuário"],
    n_results=4,
)

# A resposta pode ser enriquecida com base na tabela documents:
# Se o status for 'active', a IA pode confiar no chunk.
# Se for 'stale', a IA é instruída a buscar online ou avisar o usuário que a informação pode estar desatualizada.
```

---

## Diagrama de Relacionamento Simplificado

```
users ──┬──> areas (via area_subscriptions)
        ├──> sessions (via messages)
        │         └──> usage_logs (contagem de tokens)
        └──> area_subscriptions
                              └──> areas

documents ──> areas (via area_id)
      └── content_text: TEXT LARGE (armazena o texto completo do arquivo ou página web extraída)
      └── url: Endereço da URL original (se for documento externo/link)
      └── is_external_link: BOOLEAN (identifica se é um documento externo/link)

Vector DB ──> areas (via vector_ref)
```