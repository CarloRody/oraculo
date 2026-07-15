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
