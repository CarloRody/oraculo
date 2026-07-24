"""Database migrations for o WhatsApp Agent — tabelas whatsapp_*.

Mesmo padrão de oraculo_monitoragent/db_migrations.py e
backup-manager/db_migrations.py: lista de migrações idempotentes
(IF NOT EXISTS), aplicadas na subida do processo, dentro do mesmo banco
compartilhado (ai_tutor_db) dos outros serviços.
"""

import psycopg2

from config import DB_CONFIG


def get_db():
    return psycopg2.connect(**DB_CONFIG)


MIGRATIONS = [
    # 1 — contas conectadas (QR Code via Evolution API, ou Business API da Meta)
    """
    CREATE TABLE IF NOT EXISTS whatsapp_accounts (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        label VARCHAR(100) NOT NULL,
        connection_type VARCHAR(20) NOT NULL DEFAULT 'qrcode'
            CHECK (connection_type IN ('qrcode', 'business_api')),
        phone_number VARCHAR(20),
        area_id INTEGER REFERENCES areas(id) ON DELETE SET NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'disconnected'
            CHECK (status IN ('disconnected', 'connecting', 'qr_pending', 'connected', 'error')),
        wa_session_name VARCHAR(100),
        meta_phone_number_id VARCHAR(50),
        meta_waba_id VARCHAR(50),
        meta_access_token_enc TEXT,
        webhook_verify_token VARCHAR(100),
        ai_auto_reply_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        last_connected_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    # 2 — histórico de sessões/QR por conta
    """
    CREATE TABLE IF NOT EXISTS whatsapp_sessions (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        qr_code_base64 TEXT,
        qr_generated_at TIMESTAMPTZ,
        qr_expires_at TIMESTAMPTZ,
        connected_at TIMESTAMPTZ,
        disconnected_at TIMESTAMPTZ,
        disconnect_reason TEXT,
        session_data_ref TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_sessions_account ON whatsapp_sessions(account_id, created_at DESC);",
    # 3 — contatos
    """
    CREATE TABLE IF NOT EXISTS whatsapp_contacts (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        wa_id VARCHAR(30) NOT NULL,
        name VARCHAR(150),
        push_name VARCHAR(150),
        profile_pic_url TEXT,
        company VARCHAR(150),
        cpf VARCHAR(14),
        cnpj VARCHAR(18),
        city VARCHAR(100),
        state VARCHAR(2),
        owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'blocked', 'archived')),
        notes TEXT,
        opt_out BOOLEAN NOT NULL DEFAULT FALSE,
        last_interaction_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(account_id, wa_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_contacts_account ON whatsapp_contacts(account_id);",
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_contacts_last_interaction ON whatsapp_contacts(last_interaction_at DESC);",
    # 4 — tags + vínculo N:N
    """
    CREATE TABLE IF NOT EXISTS whatsapp_tags (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        name VARCHAR(50) NOT NULL,
        color VARCHAR(7) DEFAULT '#60a5fa',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(account_id, name)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS whatsapp_contact_tags (
        contact_id INTEGER NOT NULL REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES whatsapp_tags(id) ON DELETE CASCADE,
        PRIMARY KEY (contact_id, tag_id)
    );
    """,
    # 5 — grupos e membros (só QR Code)
    """
    CREATE TABLE IF NOT EXISTS whatsapp_groups (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        wa_group_id VARCHAR(40) NOT NULL,
        name VARCHAR(150),
        description TEXT,
        participants_count INTEGER DEFAULT 0,
        is_admin BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(account_id, wa_group_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS whatsapp_group_members (
        group_id INTEGER NOT NULL REFERENCES whatsapp_groups(id) ON DELETE CASCADE,
        contact_id INTEGER NOT NULL REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        is_admin BOOLEAN DEFAULT FALSE,
        joined_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (group_id, contact_id)
    );
    """,
    # 6 — conversas (thread por contato OU grupo)
    """
    CREATE TABLE IF NOT EXISTS whatsapp_chats (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        chat_type VARCHAR(10) NOT NULL CHECK (chat_type IN ('contact', 'group')),
        contact_id INTEGER REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        group_id INTEGER REFERENCES whatsapp_groups(id) ON DELETE CASCADE,
        assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        unread_count INTEGER NOT NULL DEFAULT 0,
        is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
        is_archived BOOLEAN NOT NULL DEFAULT FALSE,
        last_message_at TIMESTAMPTZ,
        last_message_preview TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        CHECK (
            (chat_type = 'contact' AND contact_id IS NOT NULL AND group_id IS NULL) OR
            (chat_type = 'group' AND group_id IS NOT NULL AND contact_id IS NULL)
        )
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_chats_account_lastmsg ON whatsapp_chats(account_id, last_message_at DESC);",
    # 7 — arquivos de mídia
    """
    CREATE TABLE IF NOT EXISTS whatsapp_files (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        mime_type VARCHAR(100),
        file_type VARCHAR(20) CHECK (file_type IN ('image', 'audio', 'video', 'document', 'sticker')),
        original_name VARCHAR(255),
        storage_path TEXT NOT NULL,
        size_bytes BIGINT,
        checksum_sha256 VARCHAR(64),
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    # 8 — mensagens
    """
    CREATE TABLE IF NOT EXISTS whatsapp_messages (
        id SERIAL PRIMARY KEY,
        chat_id INTEGER NOT NULL REFERENCES whatsapp_chats(id) ON DELETE CASCADE,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        wa_message_id VARCHAR(60),
        direction VARCHAR(3) NOT NULL CHECK (direction IN ('in', 'out')),
        sender_contact_id INTEGER REFERENCES whatsapp_contacts(id) ON DELETE SET NULL,
        message_type VARCHAR(20) NOT NULL DEFAULT 'text'
            CHECK (message_type IN ('text', 'image', 'audio', 'video', 'document', 'sticker', 'location', 'contact_card')),
        body TEXT,
        file_id INTEGER REFERENCES whatsapp_files(id) ON DELETE SET NULL,
        reply_to_message_id INTEGER REFERENCES whatsapp_messages(id) ON DELETE SET NULL,
        forwarded_from_message_id INTEGER REFERENCES whatsapp_messages(id) ON DELETE SET NULL,
        is_forwarded BOOLEAN NOT NULL DEFAULT FALSE,
        is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
        is_favorite BOOLEAN NOT NULL DEFAULT FALSE,
        is_ai_generated BOOLEAN NOT NULL DEFAULT FALSE,
        status VARCHAR(20) NOT NULL DEFAULT 'sent'
            CHECK (status IN ('queued', 'sent', 'delivered', 'read', 'failed')),
        failure_reason TEXT,
        sent_at TIMESTAMPTZ DEFAULT NOW(),
        delivered_at TIMESTAMPTZ,
        read_at TIMESTAMPTZ
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_chat_sent ON whatsapp_messages(chat_id, sent_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_wa_id ON whatsapp_messages(wa_message_id);",
    """
    CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_fts
        ON whatsapp_messages USING gin (to_tsvector('portuguese', coalesce(body, '')));
    """,
    # 9 — templates
    """
    CREATE TABLE IF NOT EXISTS whatsapp_templates (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        name VARCHAR(100) NOT NULL,
        category VARCHAR(20) CHECK (category IN ('marketing', 'utility', 'authentication')),
        language VARCHAR(10) DEFAULT 'pt_BR',
        body TEXT NOT NULL,
        meta_template_status VARCHAR(20),
        variables_example JSONB,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(account_id, name)
    );
    """,
    # 10 — campanhas
    """
    CREATE TABLE IF NOT EXISTS whatsapp_campaigns (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        name VARCHAR(150) NOT NULL,
        template_id INTEGER REFERENCES whatsapp_templates(id) ON DELETE SET NULL,
        segment_tag_id INTEGER REFERENCES whatsapp_tags(id) ON DELETE SET NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft', 'scheduled', 'running', 'paused', 'completed', 'cancelled', 'failed')),
        scheduled_at TIMESTAMPTZ,
        send_rate_per_minute INTEGER NOT NULL DEFAULT 10,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        total_recipients INTEGER NOT NULL DEFAULT 0,
        total_sent INTEGER NOT NULL DEFAULT 0,
        total_failed INTEGER NOT NULL DEFAULT 0,
        created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    # 11 — fila/status por destinatário de campanha
    """
    CREATE TABLE IF NOT EXISTS whatsapp_campaign_messages (
        id SERIAL PRIMARY KEY,
        campaign_id INTEGER NOT NULL REFERENCES whatsapp_campaigns(id) ON DELETE CASCADE,
        contact_id INTEGER NOT NULL REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        message_id INTEGER REFERENCES whatsapp_messages(id) ON DELETE SET NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'sent', 'delivered', 'read', 'failed', 'skipped')),
        failure_reason TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        processed_at TIMESTAMPTZ,
        UNIQUE(campaign_id, contact_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_campaign_messages_status ON whatsapp_campaign_messages(campaign_id, status);",
    # 12 — logs/auditoria
    """
    CREATE TABLE IF NOT EXISTS whatsapp_logs (
        id SERIAL PRIMARY KEY,
        account_id INTEGER REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        level VARCHAR(10) NOT NULL DEFAULT 'info' CHECK (level IN ('debug', 'info', 'warning', 'error')),
        event VARCHAR(50) NOT NULL,
        detail JSONB,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_account_created ON whatsapp_logs(account_id, created_at DESC);",
    # 13 — configurações por conta (1:1)
    """
    CREATE TABLE IF NOT EXISTS whatsapp_settings (
        account_id INTEGER PRIMARY KEY REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        business_hours JSONB,
        away_message TEXT,
        welcome_message TEXT,
        ai_system_prompt TEXT,
        max_ai_replies_per_chat_per_day INTEGER DEFAULT 20,
        notify_new_contact BOOLEAN DEFAULT TRUE,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # 14 — resposta automática por CONVERSA, não só por conta. A conta define
    # o padrão com que toda conversa nova começa (ai_auto_reply_enabled em
    # whatsapp_accounts vira só isso: um valor-padrão, não mais um gate
    # absoluto) — permite ter conexões de auto-atendimento (padrão ligado) e
    # conexões particulares (padrão desligado, só liga conversa por conversa).
    """
    ALTER TABLE whatsapp_chats ADD COLUMN IF NOT EXISTS ai_auto_reply_enabled BOOLEAN NOT NULL DEFAULT TRUE;
    """,

    # 15 — agenda de consultores: contatos promovidos a consultor (com
    # confirmação por WhatsApp antes de ficar ativo), disponibilidade semanal,
    # e os agendamentos de fato. booking_state em whatsapp_chats guarda o
    # passo atual da máquina de estados do agendamento self-service por
    # conversa (null = fora do fluxo de agendamento).
    """
    CREATE TABLE IF NOT EXISTS whatsapp_consultants (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        contact_id INTEGER NOT NULL REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        name VARCHAR(150) NOT NULL,
        context TEXT,
        slot_duration_minutes INTEGER NOT NULL DEFAULT 30,
        weekly_availability JSONB,
        reminder_hours_before INTEGER NOT NULL DEFAULT 2,
        status VARCHAR(20) NOT NULL DEFAULT 'pending_confirmation'
            CHECK (status IN ('pending_confirmation', 'active', 'declined', 'inactive')),
        confirmed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(account_id, contact_id)
    );

    CREATE TABLE IF NOT EXISTS whatsapp_appointments (
        id SERIAL PRIMARY KEY,
        consultant_id INTEGER NOT NULL REFERENCES whatsapp_consultants(id) ON DELETE CASCADE,
        client_contact_id INTEGER NOT NULL REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        scheduled_at TIMESTAMPTZ NOT NULL,
        duration_minutes INTEGER NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'confirmed'
            CHECK (status IN ('confirmed', 'cancelled', 'completed', 'no_show')),
        reminder_sent_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_whatsapp_appointments_consultant_time ON whatsapp_appointments(consultant_id, scheduled_at);
    CREATE INDEX IF NOT EXISTS idx_whatsapp_appointments_reminder ON whatsapp_appointments(scheduled_at) WHERE reminder_sent_at IS NULL AND status = 'confirmed';

    ALTER TABLE whatsapp_chats ADD COLUMN IF NOT EXISTS booking_state JSONB;
    """,

    # 16 — portal do consultor: link único (token opaco) mandado por WhatsApp,
    # sem login/senha — autentica as chamadas de /api/consultant-portal/<token>/...
    """
    ALTER TABLE whatsapp_consultants ADD COLUMN IF NOT EXISTS portal_token VARCHAR(64) UNIQUE;
    """,

    # 17 — assunto/motivo do agendamento, opcional — preenchido hoje só pelo
    # agendamento manual feito pelo próprio consultor no portal; o fluxo
    # self-service do cliente via WhatsApp não coleta isso, fica NULL nesses casos.
    """
    ALTER TABLE whatsapp_appointments ADD COLUMN IF NOT EXISTS subject VARCHAR(200);
    """,

    # 18 — nomenclatura customizável por CLIENTE (users.id), não por conta —
    # um cliente pode ter mais de uma whatsapp_accounts (o seletor de conta na
    # Agenda já existe) e a nomenclatura vale pra todas elas, por isso é
    # chaveada em user_id e não em account_id (diferente de whatsapp_settings,
    # que é por conta e está sem uso). Hoje só a chave "consultant" é escrita/
    # lida ({"singular": "...", "plural": "..."}), mas o JSONB fica aberto pra
    # dar pra acrescentar outros termos customizáveis depois sem migração nova.
    """
    CREATE TABLE IF NOT EXISTS whatsapp_client_settings (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        nomenclature JSONB NOT NULL DEFAULT '{}',
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # 19 — quem controla a disponibilidade semanal do consultor: por padrão o
    # próprio consultor (TRUE, mantém o comportamento de sempre pra quem já
    # existe), mas a empresa (admin/Área do Cliente, no cadastro do consultor)
    # pode desligar — nesse caso o card "Minha disponibilidade" some do portal
    # e só quem administra a agenda define os horários.
    """
    ALTER TABLE whatsapp_consultants ADD COLUMN IF NOT EXISTS self_availability_enabled BOOLEAN NOT NULL DEFAULT TRUE;
    """,

    # 20 — confirmação do consultor: agendamento criado pelo próprio cliente
    # via WhatsApp (self-service) agora nasce como 'pending_consultant' em vez
    # de já 'confirmed' — só vira 'confirmed' quando o consultor aperta
    # "Confirmar" no painel dele. Agendamento criado pelo PRÓPRIO consultor no
    # portal continua nascendo 'confirmed' direto (não faz sentido confirmar
    # a própria criação) — ver requires_confirmation em booking_flow.book_appointment.
    # DROP+ADD do CHECK a cada execução é o padrão idempotente aqui (mesmo
    # nome de constraint que o Postgres gera sozinho pra CHECK inline).
    """
    ALTER TABLE whatsapp_appointments DROP CONSTRAINT IF EXISTS whatsapp_appointments_status_check;
    ALTER TABLE whatsapp_appointments ADD CONSTRAINT whatsapp_appointments_status_check
        CHECK (status IN ('confirmed', 'cancelled', 'completed', 'no_show', 'pending_consultant'));
    """,

    # 21 — CRM médico / painel da secretária: checklist de acompanhamento do
    # paciente. Template é chaveado em account_id (não user_id como a
    # nomenclatura) porque cada CLÍNICA define seu próprio checklist, mesmo
    # que o mesmo dono tenha duas contas/clínicas distintas. Progresso fica
    # amarrado ao appointment_id (não ao contato) — cada consulta concluída é
    # um episódio de acompanhamento próprio, evita ambiguidade de "a qual
    # consulta esse item pertence" quando o mesmo paciente volta depois.
    """
    CREATE TABLE IF NOT EXISTS whatsapp_checklist_templates (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        step_key VARCHAR(50) NOT NULL,
        label VARCHAR(150) NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        auto_message_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        auto_message_template TEXT,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(account_id, step_key)
    );
    CREATE INDEX IF NOT EXISTS idx_checklist_templates_account
        ON whatsapp_checklist_templates(account_id, sort_order);

    CREATE TABLE IF NOT EXISTS whatsapp_checklist_items (
        id SERIAL PRIMARY KEY,
        appointment_id INTEGER NOT NULL REFERENCES whatsapp_appointments(id) ON DELETE CASCADE,
        template_id INTEGER NOT NULL REFERENCES whatsapp_checklist_templates(id) ON DELETE CASCADE,
        status VARCHAR(20) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'done', 'skipped')),
        done_at TIMESTAMPTZ,
        auto_message_sent_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(appointment_id, template_id)
    );
    CREATE INDEX IF NOT EXISTS idx_checklist_items_appointment
        ON whatsapp_checklist_items(appointment_id);
    CREATE INDEX IF NOT EXISTS idx_checklist_items_pending
        ON whatsapp_checklist_items(status) WHERE status = 'pending';

    ALTER TABLE whatsapp_appointments ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
    ALTER TABLE whatsapp_consultants ADD COLUMN IF NOT EXISTS last_weekly_summary_sent_at TIMESTAMPTZ;
    """,

    # 22 — dia/hora do resumo semanal do médico agora é configurável por
    # CLÍNICA (account_id — vale pra todos os médicos daquela conta, não por
    # médico individual), em vez de fixo em toda segunda 07h. weekday segue a
    # convenção do EXTRACT(DOW) do Postgres (0=domingo..6=sábado); default 1
    # (segunda) + 7 (07h) preserva o comportamento de hoje pra quem não mexer.
    """
    ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS weekly_summary_weekday SMALLINT NOT NULL DEFAULT 1
        CHECK (weekly_summary_weekday BETWEEN 0 AND 6);
    ALTER TABLE whatsapp_accounts ADD COLUMN IF NOT EXISTS weekly_summary_hour SMALLINT NOT NULL DEFAULT 7
        CHECK (weekly_summary_hour BETWEEN 0 AND 23);
    """,

    # 23 — CRM médico: linha do tempo de exames/documentos do paciente,
    # capturados automaticamente quando ele manda foto/PDF pelo WhatsApp.
    # Ancorada no CONTATO (contact_id), não na consulta — persiste entre
    # médicos/consultas do mesmo paciente. appointment_id é best-effort
    # (consulta mais próxima no tempo daquele contato, pode ficar NULL).
    # whatsapp_files continua sendo só o registro físico do arquivo (migração
    # #7); esta tabela é a camada semântica "isso é um documento do paciente
    # X". status permite o download da Evolution API rodar em background sem
    # travar o webhook: linha nasce 'pending', vira 'stored' (com file_id) ou
    # 'failed' (com failure_reason) depois. hidden é soft-delete — nunca
    # apaga o arquivo de verdade, só some da timeline.
    """
    CREATE TABLE IF NOT EXISTS whatsapp_patient_documents (
        id SERIAL PRIMARY KEY,
        contact_id INTEGER NOT NULL REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        file_id INTEGER REFERENCES whatsapp_files(id) ON DELETE CASCADE,
        appointment_id INTEGER REFERENCES whatsapp_appointments(id) ON DELETE SET NULL,
        message_id INTEGER REFERENCES whatsapp_messages(id) ON DELETE SET NULL,
        wa_message_id VARCHAR(60),
        doc_type VARCHAR(20) NOT NULL DEFAULT 'document'
            CHECK (doc_type IN ('image', 'document')),
        caption TEXT,
        status VARCHAR(20) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'stored', 'failed')),
        failure_reason TEXT,
        hidden BOOLEAN NOT NULL DEFAULT FALSE,
        captured_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_patient_documents_contact
        ON whatsapp_patient_documents(contact_id, captured_at DESC);
    CREATE INDEX IF NOT EXISTS idx_patient_documents_account
        ON whatsapp_patient_documents(account_id);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_patient_documents_wa_msg
        ON whatsapp_patient_documents(account_id, wa_message_id) WHERE wa_message_id IS NOT NULL;
    """,

    # 24 — checklist multi-destinatário (paciente/médico/secretária) +
    # identidade persistida da secretária. auto_message_enabled sempre
    # significou "manda pro paciente" — as 3 colunas novas substituem essa
    # semântica sem quebrar o comportamento existente (backfill abaixo).
    # auto_message_enabled e auto_message_sent_at ficam congeladas no schema
    # (nunca mais lidas/escritas pela aplicação) em vez de removidas — esta
    # base de migrações nunca fez DROP COLUMN, só ADD; manter é reversível e
    # sem custo, remover não. O UPDATE de backfill só pode rodar UMA vez (na
    # criação da coluna), senão sobrescreveria silenciosamente uma edição
    # feita pela clínica depois da migração em reinícios seguintes do
    # processo — por isso o DO $$ guardado por information_schema, não um
    # UPDATE solto. Secretária vira um contato como o médico (contact_id em
    # whatsapp_contacts), não um telefone cru — reaproveita get_or_create_contact.
    """
    ALTER TABLE whatsapp_accounts
        ADD COLUMN IF NOT EXISTS secretary_contact_id INTEGER REFERENCES whatsapp_contacts(id) ON DELETE SET NULL;

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'whatsapp_checklist_templates' AND column_name = 'notify_patient'
        ) THEN
            ALTER TABLE whatsapp_checklist_templates ADD COLUMN notify_patient BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE whatsapp_checklist_templates ADD COLUMN notify_consultant BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE whatsapp_checklist_templates ADD COLUMN notify_secretary BOOLEAN NOT NULL DEFAULT FALSE;
            UPDATE whatsapp_checklist_templates SET notify_patient = TRUE WHERE auto_message_enabled = TRUE;
        END IF;
    END $$;

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'whatsapp_checklist_items' AND column_name = 'auto_message_sent_patient_at'
        ) THEN
            ALTER TABLE whatsapp_checklist_items ADD COLUMN auto_message_sent_patient_at TIMESTAMPTZ;
            ALTER TABLE whatsapp_checklist_items ADD COLUMN auto_message_sent_consultant_at TIMESTAMPTZ;
            ALTER TABLE whatsapp_checklist_items ADD COLUMN auto_message_sent_secretary_at TIMESTAMPTZ;
            UPDATE whatsapp_checklist_items SET auto_message_sent_patient_at = auto_message_sent_at
                WHERE auto_message_sent_at IS NOT NULL;
        END IF;
    END $$;
    """,

    # 25 — ficha completa do paciente: cadastro/dados clínicos básicos (1:1
    # com o contato, contact_id como PK) e linha do tempo de evolução do
    # tratamento. Ancoradas em contact_id como whatsapp_patient_documents
    # (migração #23) — persistem entre médicos/consultas do mesmo paciente.
    # Todos os campos de whatsapp_patient_records são opcionais: cadastro é
    # preenchido aos poucos. whatsapp_contacts.name (migração #3, nunca
    # escrita até hoje) passa a ser gravada por esta feature — reaproveitada
    # em vez de duplicada aqui. Notas de evolução são append-only (só
    # cria/lista, nunca edita/apaga — prontuário não se reescreve), e podem
    # vir da secretária OU do médico: nota de secretária sempre tem
    # consultant_id NULL (exibida como "Secretária" — só existe UMA
    # secretária por conta, whatsapp_accounts.secretary_contact_id, não vale
    # a pena um sistema de identidade individual pra isso); nota de médico
    # sempre tem consultant_id preenchido (exibida com o nome dele). O CHECK
    # garante essa consistência no banco. account_id fica denormalizado nas
    # duas tabelas, mesmo padrão de whatsapp_patient_documents, pra permitir
    # guards diretos sem JOIN extra.
    """
    CREATE TABLE IF NOT EXISTS whatsapp_patient_records (
        contact_id INTEGER PRIMARY KEY REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        birth_date DATE,
        address TEXT,
        emergency_contact_name VARCHAR(150),
        emergency_contact_phone VARCHAR(20),
        allergies TEXT,
        medications_in_use TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_patient_records_account ON whatsapp_patient_records(account_id);

    CREATE TABLE IF NOT EXISTS whatsapp_patient_evolution_notes (
        id SERIAL PRIMARY KEY,
        contact_id INTEGER NOT NULL REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        appointment_id INTEGER REFERENCES whatsapp_appointments(id) ON DELETE SET NULL,
        consultant_id INTEGER REFERENCES whatsapp_consultants(id) ON DELETE SET NULL,
        author_type VARCHAR(20) NOT NULL CHECK (author_type IN ('secretary', 'consultant')),
        note TEXT NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        CHECK (
            (author_type = 'consultant' AND consultant_id IS NOT NULL) OR
            (author_type = 'secretary' AND consultant_id IS NULL)
        )
    );
    CREATE INDEX IF NOT EXISTS idx_patient_evolution_notes_contact
        ON whatsapp_patient_evolution_notes(contact_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_patient_evolution_notes_account
        ON whatsapp_patient_evolution_notes(account_id);
    """,

    # 26 — CPF no cadastro do paciente (campo de texto livre, sem validação
    # de dígito verificador nem unicidade — mesmo espírito dos outros campos
    # de whatsapp_patient_records).
    """
    ALTER TABLE whatsapp_patient_records ADD COLUMN IF NOT EXISTS cpf VARCHAR(14);
    """,

    # 27 — registro de atendimento por agendamento (detalhes da consulta,
    # diagnóstico, prescrição), preenchido pelo médico no portal dele ao
    # "iniciar atendimento". 1:1 com o agendamento (appointment_id como PK),
    # mesmo padrão de whatsapp_patient_records (migração #25). contact_id/
    # account_id denormalizados pra guards diretos sem JOIN extra.
    """
    CREATE TABLE IF NOT EXISTS whatsapp_appointment_consultations (
        appointment_id INTEGER PRIMARY KEY REFERENCES whatsapp_appointments(id) ON DELETE CASCADE,
        contact_id INTEGER NOT NULL REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        consultant_id INTEGER NOT NULL REFERENCES whatsapp_consultants(id) ON DELETE CASCADE,
        notes TEXT,
        diagnosis TEXT,
        prescription TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_appointment_consultations_contact
        ON whatsapp_appointment_consultations(contact_id);
    CREATE INDEX IF NOT EXISTS idx_appointment_consultations_account
        ON whatsapp_appointment_consultations(account_id);
    """,

    # 28 — cadastro do médico: CPF, CRM, endereço, telefone alternativo,
    # especialidades. Campos de texto livre (mesmo espírito dos outros
    # cadastros da plataforma), editáveis tanto pela secretária (painel dela)
    # quanto pelo próprio médico (Minha Agenda, aba Configurações).
    """
    ALTER TABLE whatsapp_consultants ADD COLUMN IF NOT EXISTS cpf VARCHAR(14);
    ALTER TABLE whatsapp_consultants ADD COLUMN IF NOT EXISTS crm VARCHAR(20);
    ALTER TABLE whatsapp_consultants ADD COLUMN IF NOT EXISTS address TEXT;
    ALTER TABLE whatsapp_consultants ADD COLUMN IF NOT EXISTS alt_phone VARCHAR(20);
    ALTER TABLE whatsapp_consultants ADD COLUMN IF NOT EXISTS specialties TEXT;
    """,

    # 29 — consentimento LGPD antes de capturar documentos de um contato.
    # lgpd_consent_status é o status atual (consultado no caminho quente do
    # webhook, sem JOIN extra); whatsapp_lgpd_consents é o histórico
    # append-only de cada pedido, com o JSON cru da Evolution API (pedido e
    # resposta) guardado como prova do consentimento. Todo contato nasce
    # 'none' — inclusive quem já tinha documento capturado antes desta
    # feature existir (decisão: todo mundo precisa responder de novo).
    """
    ALTER TABLE whatsapp_contacts ADD COLUMN IF NOT EXISTS lgpd_consent_status VARCHAR(20) NOT NULL DEFAULT 'none'
        CHECK (lgpd_consent_status IN ('none', 'pending', 'accepted', 'declined'));

    CREATE TABLE IF NOT EXISTS whatsapp_lgpd_consents (
        id SERIAL PRIMARY KEY,
        contact_id INTEGER NOT NULL REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        consent_text TEXT NOT NULL,
        trigger_raw_payload JSONB,
        requested_at TIMESTAMPTZ DEFAULT NOW(),
        response VARCHAR(20) CHECK (response IN ('accepted', 'declined')),
        response_text TEXT,
        response_wa_message_id VARCHAR(60),
        response_raw_payload JSONB,
        responded_at TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS idx_lgpd_consents_contact ON whatsapp_lgpd_consents(contact_id, requested_at DESC);
    CREATE INDEX IF NOT EXISTS idx_lgpd_consents_account ON whatsapp_lgpd_consents(account_id);
    """,

    # 30 — dados biológicos fixos do paciente (não mudam a cada consulta,
    # diferente da biometria da migração #31) — junto do cadastro existente
    # em whatsapp_patient_records (migração #25).
    """
    ALTER TABLE whatsapp_patient_records ADD COLUMN IF NOT EXISTS biological_sex VARCHAR(10)
        CHECK (biological_sex IN ('male', 'female'));
    ALTER TABLE whatsapp_patient_records ADD COLUMN IF NOT EXISTS blood_type VARCHAR(3)
        CHECK (blood_type IN ('A+','A-','B+','B-','AB+','AB-','O+','O-'));
    """,

    # 31 — biometria por consulta (peso, pressão, etc — muda a cada visita).
    # Tabela tipo+valor (não colunas fixas) pra permitir novos tipos de
    # medida no futuro sem alterar o schema de novo. UNIQUE(appointment_id,
    # measurement_type) permite upsert natural por medida. IMC não é
    # guardado — é calculado a partir de weight_kg/height_cm da mesma
    # consulta na hora de exibir, pra nunca ficar dessincronizado.
    """
    CREATE TABLE IF NOT EXISTS whatsapp_consultation_biometrics (
        id SERIAL PRIMARY KEY,
        appointment_id INTEGER NOT NULL REFERENCES whatsapp_appointments(id) ON DELETE CASCADE,
        contact_id INTEGER NOT NULL REFERENCES whatsapp_contacts(id) ON DELETE CASCADE,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        measurement_type VARCHAR(30) NOT NULL CHECK (measurement_type IN (
            'weight_kg', 'height_cm', 'blood_pressure_systolic', 'blood_pressure_diastolic',
            'heart_rate_bpm', 'glucose_mg_dl', 'temperature_c'
        )),
        value NUMERIC(6,2) NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (appointment_id, measurement_type)
    );
    CREATE INDEX IF NOT EXISTS idx_consultation_biometrics_contact
        ON whatsapp_consultation_biometrics(contact_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_consultation_biometrics_appointment
        ON whatsapp_consultation_biometrics(appointment_id);
    """,

    # 32 — construtor de fluxo de atendimento automático (contas em modo
    # Consultores): fluxos nomeados por conta (cada um com suas próprias
    # palavras-gatilho) e as etapas de cada fluxo. Mesmo espírito de
    # whatsapp_checklist_templates/_items (migração #21): soft-delete via
    # active=FALSE pra nunca quebrar referências já em uso. flow_state em
    # whatsapp_chats guarda a posição atual de CADA conversa dentro de um
    # fluxo (qual fluxo, qual etapa, histórico pra "voltar", variáveis
    # capturadas) — mesmo padrão do booking_state já existente, usado pelo
    # motor de agendamento (booking_flow.py).
    """
    CREATE TABLE IF NOT EXISTS whatsapp_flows (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES whatsapp_accounts(id) ON DELETE CASCADE,
        name VARCHAR(100) NOT NULL,
        trigger_keywords JSONB NOT NULL DEFAULT '[]',
        is_default BOOLEAN NOT NULL DEFAULT FALSE,
        sort_order INTEGER NOT NULL DEFAULT 0,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_flows_account ON whatsapp_flows(account_id, sort_order);

    CREATE TABLE IF NOT EXISTS whatsapp_flow_steps (
        id SERIAL PRIMARY KEY,
        flow_id INTEGER NOT NULL REFERENCES whatsapp_flows(id) ON DELETE CASCADE,
        step_key VARCHAR(50) NOT NULL,
        is_root BOOLEAN NOT NULL DEFAULT FALSE,
        step_type VARCHAR(20) NOT NULL CHECK (step_type IN ('message','menu','collect_input','action')),
        label VARCHAR(150) NOT NULL,
        message_template TEXT,
        variable_name VARCHAR(50),
        action_type VARCHAR(20) CHECK (action_type IN ('start_booking','human_handoff','faq_ai','end')),
        options JSONB,
        next_step_key VARCHAR(50),
        sort_order INTEGER NOT NULL DEFAULT 0,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(flow_id, step_key)
    );
    CREATE INDEX IF NOT EXISTS idx_flow_steps_flow ON whatsapp_flow_steps(flow_id, sort_order);

    ALTER TABLE whatsapp_chats ADD COLUMN IF NOT EXISTS flow_state JSONB;
    """,
]


def migrate_if_needed():
    conn = get_db()
    try:
        cur = conn.cursor()
        for sql in MIGRATIONS:
            cur.execute(sql.strip())
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Migration error: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate_if_needed()
    print("Migrations complete.")
