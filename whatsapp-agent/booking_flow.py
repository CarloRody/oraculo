"""Fluxo de agendamento self-service via WhatsApp — máquina de estados por
conversa, guardada em whatsapp_chats.booking_state. Chamado pelo webhook em
server.py (webhook_evolution).

Importa server.py só DENTRO das funções (import tardio), nunca no topo do
arquivo — server.py importa este módulo, então um "import server" no topo
daqui criaria um ciclo. Como o acesso só acontece quando as funções são
chamadas de verdade (depois que o app já terminou de subir), isso é seguro:
ver docs internas do Python sobre import parcial de módulo em ciclo.

Horários de disponibilidade (weekly_availability) são interpretados no fuso
America/Sao_Paulo — não há como configurar isso por enquanto (não foi pedido),
mas é o único lugar que precisaria mudar se algum dia for necessário.

Pensado pro caso de uso de uma clínica médica: quem está do outro lado pode
estar com pressa, nervoso ou com dificuldade de digitar. Por isso:
- toda comparação de texto passa por _normalize (sem acento, minúsculo) e
  aceita pequeno erro de digitação via _fuzzy_match (difflib, stdlib);
- "cancelar"/"ajuda" funcionam a qualquer momento do fluxo, sem perder o
  progresso no caso de "ajuda";
- um erro de digitação na lista não cancela mais o fluxo de cara — dá até
  3 tentativas, reenviando a lista;
- mensagens com sinal de urgência ("urgente", "o quanto antes"...) entram
  num modo que mostra um aviso de segurança fixo (procurar 192/pronto-socorro
  em caso de emergência grave — não tentamos detectar sintoma nenhum) e
  oferece só os 1-2 horários mais próximos, em vez de lista longa.
"""

import datetime
import difflib
import unicodedata
from zoneinfo import ZoneInfo

import psycopg2

from config import DB_CONFIG
from connectors.evolution import EvolutionError
import connectors.evolution as evolution

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

TRIGGER_PHRASES = ("marcar horario", "marcar consulta", "preciso de atendimento")
TRIGGER_WORDS = ("agendar", "consulta", "atendimento", "horario")
URGENCY_PHRASES = ("o quanto antes", "hoje mesmo", "mal estar", "passando mal")
URGENCY_WORDS = ("urgente", "urgencia", "emergencia", "socorro")
CANCEL_WORDS = ("cancelar", "sair", "parar", "desistir")
HELP_PHRASES = ("nao entendi",)
HELP_WORDS = ("ajuda", "menu", "atendente")

YES_WORDS = {"sim", "s", "confirmar", "confirmo", "yes", "1", "pode ser", "claro", "ok", "beleza", "blz", "isso"}
NO_WORDS = {"nao", "n", "cancelar", "no", "2", "nao quero", "deixa pra la", "nao posso"}

_NUMBER_WORDS = {
    "um": 1, "uma": 1, "primeiro": 1, "primeira": 1,
    "dois": 2, "segundo": 2, "segunda": 2,
    "tres": 3, "terceiro": 3, "terceira": 3,
    "quatro": 4, "quarto": 4, "quarta": 4,
    "cinco": 5, "quinto": 5, "quinta": 5,
    "seis": 6, "sexto": 6, "sexta": 6,
    "sete": 7, "setimo": 7, "setima": 7,
    "oito": 8, "oitavo": 8, "oitava": 8,
    "nove": 9, "nono": 9, "nona": 9,
    "dez": 10, "decimo": 10, "decima": 10,
}


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def _phone(wa_id):
    return (wa_id or "").split("@")[0]


def _term(account, plural=False):
    """Nome customizável de 'consultor' pra esse cliente (ex: 'Médico',
    'Especialista') — configurado em whatsapp_client_settings, vale pra todas
    as contas do mesmo cliente. Sem configuração, cai no padrão 'Consultor'/
    'Consultores'."""
    import server
    nomenclature = server.get_nomenclature(account.get("user_id"))
    form = "plural" if plural else "singular"
    return nomenclature["consultant"][form]


def _normalize(text):
    """Minúsculo, sem acento, sem espaço nas pontas — base pra toda
    comparação de texto tolerante a erro deste módulo."""
    t = (text or "").strip().lower()
    t = unicodedata.normalize("NFKD", t)
    return "".join(ch for ch in t if not unicodedata.combining(ch))


def _fuzzy_match(text, candidates, cutoff=0.75):
    """Compara text (já normalizado) contra uma lista de palavras/frases-alvo,
    tolerando pequeno erro de digitação (1-2 letras trocadas/faltando).
    Devolve o candidato mais parecido ou None."""
    if not text or not candidates:
        return None
    matches = difflib.get_close_matches(text, candidates, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def _matches_any(text, phrases=(), words=()):
    t = _normalize(text)
    if any(p in t for p in phrases):
        return True
    if not words:
        return False
    tokens = t.split()
    return any(_fuzzy_match(tok, words) for tok in tokens)


def _is_trigger(text):
    return _matches_any(text, TRIGGER_PHRASES, TRIGGER_WORDS)


def _is_urgent(text):
    return _matches_any(text, URGENCY_PHRASES, URGENCY_WORDS)


def _is_cancel(text):
    return _matches_any(text, words=CANCEL_WORDS)


def _is_help(text):
    return _matches_any(text, HELP_PHRASES, HELP_WORDS)


def parse_yes_no(text):
    """Fallback de texto pra quando o botão nativo do WhatsApp (viewOnceMessage/
    nativeFlowMessage, formato usado por evolution.send_buttons) não é exibido
    no aparelho do destinatário — usado tanto aqui quanto em server.py pra
    confirmação de cadastro de consultor. Aceita variações coloquiais e
    pequeno erro de digitação nas palavras mais longas (evita tolerância a
    erro nos atalhos de 1 letra, tipo "s"/"n", pra não dar falso positivo).
    None = não reconheceu como sim/não."""
    t = _normalize(text)
    if t in YES_WORDS:
        return True
    if t in NO_WORDS:
        return False
    safe_yes = [w for w in YES_WORDS if len(w) > 2]
    safe_no = [w for w in NO_WORDS if len(w) > 2]
    if _fuzzy_match(t, safe_yes, cutoff=0.8):
        return True
    if _fuzzy_match(t, safe_no, cutoff=0.8):
        return False
    return None


def _parse_index(text, options, names=None):
    """Fallback de texto pra resposta de lista — 'digite o número da opção'.
    Aceita o dígito puro (com pontuação/espaço ao redor, ex: '1)', '1.'), por
    extenso ('um', 'primeiro'...), e — quando names é passado (lista de nomes
    na mesma ordem de options, usado na escolha de consultor) — o nome
    digitado, mesmo com erro de digitação, via correspondência aproximada.
    1-based na mensagem, devolve o índice 0-based em options."""
    if not options:
        return None
    t = _normalize(text).strip(" .)-")
    if t.isdigit():
        i = int(t) - 1
        return i if 0 <= i < len(options) else None
    if t in _NUMBER_WORDS:
        i = _NUMBER_WORDS[t] - 1
        return i if 0 <= i < len(options) else None
    if names:
        normalized_names = [_normalize(n) for n in names]
        # nome digitado parcial (ex: só o primeiro nome) bate por substring
        # antes de tentar aproximação por erro de digitação
        for i, n in enumerate(normalized_names):
            if t and n and (t in n or n in t):
                return i
        match = _fuzzy_match(t, [n for n in normalized_names if n], cutoff=0.6)
        if match is not None:
            return normalized_names.index(match)
    return None


def compute_free_slots(consultant, days_ahead=14, limit=10):
    """Próximos horários livres do consultor, cruzando weekly_availability
    (JSONB, ex: {"mon": [["09:00","12:00"]]}) com os agendamentos já
    confirmados. Retorna datetimes com timezone (America/Sao_Paulo).

    weekly_availability também aceita chaves de data ISO ("2026-08-15") ao
    lado das chaves de dia da semana — datas avulsas, cadastradas pelo
    consultor pra liberar um horário fora do padrão semanal normal. Os dois
    tipos de chave se SOMAM (uma data avulsa nunca substitui o padrão da
    semana, só adiciona mais intervalo naquele dia)."""
    availability = consultant.get("weekly_availability") or {}
    if not availability:
        return []
    duration = datetime.timedelta(minutes=consultant["slot_duration_minutes"])
    now = datetime.datetime.now(LOCAL_TZ)

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT scheduled_at, duration_minutes FROM whatsapp_appointments
               WHERE consultant_id = %s AND status IN ('confirmed', 'pending_consultant') AND scheduled_at >= %s""",
            (consultant["id"], now),
        )
        busy = [(r[0], r[0] + datetime.timedelta(minutes=r[1])) for r in cur.fetchall()]
    finally:
        conn.close()

    slots = []
    day = now.date()
    for _ in range(days_ahead):
        key = WEEKDAY_KEYS[day.weekday()]
        ranges = list(availability.get(key, [])) + list(availability.get(day.isoformat(), []))
        for start_str, end_str in ranges:
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))
            slot_start = datetime.datetime.combine(day, datetime.time(start_h, start_m), tzinfo=LOCAL_TZ)
            day_end = datetime.datetime.combine(day, datetime.time(end_h, end_m), tzinfo=LOCAL_TZ)
            while slot_start + duration <= day_end:
                overlaps = any(slot_start < b_end and slot_start + duration > b_start for b_start, b_end in busy)
                if slot_start > now and not overlaps:
                    slots.append(slot_start)
                    if len(slots) >= limit:
                        return slots
                slot_start += duration
        day += datetime.timedelta(days=1)
    return slots


def _create_appointment_if_free(consultant, client_contact_id, scheduled_at, subject=None, status="confirmed"):
    """pg_advisory_xact_lock serializa tentativas de agendar o MESMO
    consultor — evita duas pessoas confirmarem o mesmo horário ao mesmo
    tempo (checar disponibilidade e inserir não são atômicos sem isso).

    O conflito é checado contra 'confirmed' E 'pending_consultant' — um
    horário aguardando confirmação do consultor já fica reservado, ninguém
    mais pode escolhê-lo enquanto isso (senão dois clientes poderiam disputar
    o mesmo horário até o consultor decidir)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (consultant["id"],))
        duration = consultant["slot_duration_minutes"]
        end_at = scheduled_at + datetime.timedelta(minutes=duration)
        cur.execute(
            """SELECT 1 FROM whatsapp_appointments
               WHERE consultant_id = %s AND status IN ('confirmed', 'pending_consultant')
               AND scheduled_at < %s AND (scheduled_at + make_interval(mins => duration_minutes)) > %s""",
            (consultant["id"], end_at, scheduled_at),
        )
        if cur.fetchone():
            conn.rollback()
            return False
        cur.execute(
            """INSERT INTO whatsapp_appointments (consultant_id, client_contact_id, scheduled_at, duration_minutes, subject, status)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (consultant["id"], client_contact_id, scheduled_at, duration, subject, status),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def book_appointment(consultant, client_contact_id, client_wa_id, client_push_name, scheduled_at, notify_consultant=False, subject=None, requires_confirmation=False):
    """Cria o agendamento (se o horário ainda estiver livre) e avisa o
    cliente. Usado tanto pelo fluxo self-service do cliente (notify_consultant
    =True, ele ainda não sabe do agendamento) quanto pelo portal do próprio
    consultor (notify_consultant=False — não faz sentido avisar quem tá
    criando). `subject` é opcional — só o portal do consultor coleta isso hoje.

    `requires_confirmation=True` (usado só no fluxo self-service do cliente)
    cria o agendamento como 'pending_consultant' em vez de 'confirmed' direto
    — o cliente é avisado que está aguardando, e o consultor recebe o link do
    painel pra confirmar (botão "Confirmar", ver server.py). Quando o próprio
    consultor cria pelo portal, não faz sentido esperar confirmação da
    própria criação, por isso o padrão é False.

    Retorna True se criou, False se o horário já não estava livre."""
    status = "pending_consultant" if requires_confirmation else "confirmed"
    if not _create_appointment_if_free(consultant, client_contact_id, scheduled_at, subject=subject, status=status):
        return False
    when = scheduled_at.strftime("%d/%m às %H:%M")
    client_phone = _phone(client_wa_id)
    subject_line = f"\nAssunto: {subject}" if subject else ""
    if requires_confirmation:
        client_msg = (f"Recebi seu pedido de agendamento com {consultant['name']} em {when}!{subject_line}\n"
                       f"Assim que o profissional confirmar, eu te aviso por aqui.")
    else:
        client_msg = f"Você tem um agendamento confirmado com {consultant['name']} em {when}!{subject_line}"
    try:
        evolution.send_text(consultant["wa_session_name"], client_phone, client_msg)
    except EvolutionError:
        pass
    if notify_consultant:
        if requires_confirmation:
            import server  # tardio — ver docstring do módulo
            consultant_msg = (f"Novo agendamento pra confirmar: {client_push_name or client_phone} em {when}.{subject_line}\n"
                               f"Confirme no seu painel: {server.portal_link(consultant['portal_token'])}")
        else:
            consultant_msg = f"Novo agendamento: {client_push_name or client_phone} em {when}.{subject_line}"
        try:
            evolution.send_text(consultant["wa_session_name"], _phone(consultant["wa_id"]), consultant_msg)
        except EvolutionError:
            pass
    return True


def _get_appointment_full(appointment_id):
    """Agendamento + dados do consultor/cliente já resolvidos numa consulta
    só — usado pelas ações do portal do consultor (cancelar/remarcar), que
    precisam de tudo isso pra montar o aviso mandado ao cliente."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT a.id, a.consultant_id, con.name, con.slot_duration_minutes, acc.wa_session_name,
                      a.client_contact_id, ct.wa_id, ct.push_name, a.scheduled_at, a.duration_minutes, a.status
               FROM whatsapp_appointments a
               JOIN whatsapp_consultants con ON con.id = a.consultant_id
               JOIN whatsapp_accounts acc ON acc.id = con.account_id
               JOIN whatsapp_contacts ct ON ct.id = a.client_contact_id
               WHERE a.id = %s""",
            (appointment_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = ["id", "consultant_id", "consultant_name", "slot_duration_minutes", "wa_session_name",
                "client_contact_id", "client_wa_id", "client_push_name", "scheduled_at", "duration_minutes", "status"]
        return dict(zip(cols, row))
    finally:
        conn.close()


def cancel_appointment_and_notify(appointment_id, consultant_id):
    """Cancela (só se pertencer ao consultor_id informado — o portal nunca
    deixa mexer no agendamento de outro consultor) e avisa o cliente.
    Retorna 'ok', 'not_found' ou 'forbidden'."""
    appt = _get_appointment_full(appointment_id)
    if not appt:
        return "not_found"
    if appt["consultant_id"] != consultant_id:
        return "forbidden"
    if appt["status"] != "confirmed":
        return "not_found"
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_appointments SET status = 'cancelled' WHERE id = %s", (appointment_id,))
        conn.commit()
    finally:
        conn.close()
    when = appt["scheduled_at"].astimezone(LOCAL_TZ).strftime("%d/%m às %H:%M")
    try:
        evolution.send_text(appt["wa_session_name"], _phone(appt["client_wa_id"]),
                             f"Seu agendamento com {appt['consultant_name']} em {when} foi cancelado.")
    except EvolutionError:
        pass
    return "ok"


def confirm_appointment_and_notify(appointment_id, consultant_id):
    """Confirma um agendamento que veio do self-service do cliente e estava
    aguardando o consultor ('pending_consultant') e avisa o cliente. Retorna
    'ok', 'not_found' ou 'forbidden'."""
    appt = _get_appointment_full(appointment_id)
    if not appt:
        return "not_found"
    if appt["consultant_id"] != consultant_id:
        return "forbidden"
    if appt["status"] != "pending_consultant":
        return "not_found"
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_appointments SET status = 'confirmed' WHERE id = %s", (appointment_id,))
        conn.commit()
    finally:
        conn.close()
    when = appt["scheduled_at"].astimezone(LOCAL_TZ).strftime("%d/%m às %H:%M")
    try:
        evolution.send_text(appt["wa_session_name"], _phone(appt["client_wa_id"]),
                             f"Seu agendamento com {appt['consultant_name']} em {when} foi confirmado pelo profissional! ✅")
    except EvolutionError:
        pass
    return "ok"


def decline_appointment_and_notify(appointment_id, consultant_id):
    """Recusa um agendamento que estava aguardando confirmação do consultor
    ('pending_consultant') — libera o horário (vira 'cancelled', mesmo estado
    final de um cancelamento normal) e avisa o cliente. Retorna 'ok',
    'not_found' ou 'forbidden'."""
    appt = _get_appointment_full(appointment_id)
    if not appt:
        return "not_found"
    if appt["consultant_id"] != consultant_id:
        return "forbidden"
    if appt["status"] != "pending_consultant":
        return "not_found"
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_appointments SET status = 'cancelled' WHERE id = %s", (appointment_id,))
        conn.commit()
    finally:
        conn.close()
    when = appt["scheduled_at"].astimezone(LOCAL_TZ).strftime("%d/%m às %H:%M")
    try:
        evolution.send_text(appt["wa_session_name"], _phone(appt["client_wa_id"]),
                             f"Seu pedido de agendamento com {appt['consultant_name']} em {when} não pôde ser confirmado. "
                             f"Digite \"agendar\" pra escolher outro horário.")
    except EvolutionError:
        pass
    return "ok"


def reschedule_appointment_and_notify(appointment_id, consultant_id, new_scheduled_at):
    """Remarca (revalidando disponibilidade, mesma trava de concorrência do
    agendamento novo) e avisa o cliente com o horário antigo e o novo.
    Retorna 'ok', 'not_found', 'forbidden' ou 'conflict'."""
    appt = _get_appointment_full(appointment_id)
    if not appt:
        return "not_found"
    if appt["consultant_id"] != consultant_id:
        return "forbidden"
    if appt["status"] != "confirmed":
        return "not_found"

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (consultant_id,))
        end_at = new_scheduled_at + datetime.timedelta(minutes=appt["slot_duration_minutes"])
        cur.execute(
            """SELECT 1 FROM whatsapp_appointments
               WHERE consultant_id = %s AND status IN ('confirmed', 'pending_consultant') AND id != %s
               AND scheduled_at < %s AND (scheduled_at + make_interval(mins => duration_minutes)) > %s""",
            (consultant_id, appointment_id, end_at, new_scheduled_at),
        )
        if cur.fetchone():
            conn.rollback()
            return "conflict"
        cur.execute(
            "UPDATE whatsapp_appointments SET scheduled_at = %s, reminder_sent_at = NULL WHERE id = %s",
            (new_scheduled_at, appointment_id),
        )
        conn.commit()
    finally:
        conn.close()

    old_when = appt["scheduled_at"].astimezone(LOCAL_TZ).strftime("%d/%m às %H:%M")
    new_when = new_scheduled_at.strftime("%d/%m às %H:%M")
    try:
        evolution.send_text(appt["wa_session_name"], _phone(appt["client_wa_id"]),
                             f"Seu agendamento com {appt['consultant_name']} foi remarcado de {old_when} para {new_when}.")
    except EvolutionError:
        pass
    return "ok"


def handle_incoming(account, chat_id, contact_id, wa_id, text, selected_id, push_name=None):
    """Ponto de entrada chamado pelo webhook pra cada mensagem recebida (já
    depois de descartar cliques de confirmação de consultor, tratados à
    parte em server.py). Retorna True se a mensagem foi absorvida por este
    fluxo (o webhook não deve cair pra IA/medição de recebida-sem-área nesse
    caso), False se não tem nada a ver com agendamento."""
    import server  # tardio — ver docstring do módulo

    state = server.get_chat_booking_state(chat_id)
    phone = _phone(wa_id)

    if state is None:
        if not _is_trigger(text):
            return False
        if not server.plan_has_agenda(account.get("user_id")):
            return False
        consultants = [c for c in server.get_consultants(account["id"]) if c["status"] == "active"]
        if not consultants:
            return False
        urgent = _is_urgent(text)
        option_ids = _send_consultant_list(account, wa_id, consultants, urgent=urgent)
        server.set_chat_booking_state(chat_id, {"step": "choosing_consultant", "options": option_ids, "urgent": urgent})
        return True

    # Comandos globais de "cancelar"/"ajuda" — só interceptam quando já existe
    # uma conversa de agendamento em andamento, e só pra respostas digitadas
    # (um clique de botão/lista nunca deve ser confundido com esses comandos).
    if not selected_id and _is_cancel(text):
        server.set_chat_booking_state(chat_id, None)
        _send_text(account, phone, "Sem problema, cancelei o agendamento por aqui. Quando quiser, é só me chamar de novo e digitar \"agendar\".")
        return True
    if not selected_id and _is_help(text):
        new_state = _resend_current_step(account, wa_id, state)
        if new_state is None:
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, f"{_term(account)} não encontrado. Digite \"agendar\" pra começar de novo.")
        else:
            server.set_chat_booking_state(chat_id, new_state)
        return True

    step = state.get("step")
    urgent = bool(state.get("urgent"))

    if step == "choosing_consultant":
        consultant_id = None
        if selected_id and selected_id.startswith("consultant_"):
            consultant_id = int(selected_id.split("_")[1])
        else:
            idx = _parse_index(text, state.get("options"))
            if idx is None:
                names = [(server.get_consultant(cid) or {}).get("name", "") for cid in state.get("options", [])]
                idx = _parse_index(text, state.get("options"), names=names)
            if idx is not None:
                consultant_id = state["options"][idx]
        if consultant_id is None:
            _retry_or_give_up(account, chat_id, wa_id, phone, state)
            return True
        consultant = server.get_consultant(consultant_id)
        if not consultant or consultant["account_id"] != account["id"] or consultant["status"] != "active":
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, f"{_term(account)} não encontrado. Digite \"agendar\" pra começar de novo.")
            return True
        slots = compute_free_slots(consultant, limit=2 if urgent else 10)
        if not slots:
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, f"{consultant['name']} não tem horários livres nos próximos dias. Tente de novo mais tarde.")
            return True
        option_isos = _send_slot_list(account, wa_id, consultant, slots)
        server.set_chat_booking_state(chat_id, {"step": "choosing_slot", "consultant_id": consultant_id, "options": option_isos, "urgent": urgent})
        return True

    if step == "choosing_slot":
        iso = None
        if selected_id and selected_id.startswith("slot_"):
            _, _consultant_id_str, iso = selected_id.split("_", 2)
        else:
            idx = _parse_index(text, state.get("options"))
            if idx is not None:
                iso = state["options"][idx]
        if iso is None:
            _retry_or_give_up(account, chat_id, wa_id, phone, state)
            return True
        consultant = server.get_consultant(state["consultant_id"])
        if not consultant:
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, f"{_term(account)} não encontrado. Digite \"agendar\" pra começar de novo.")
            return True
        scheduled_at = datetime.datetime.fromisoformat(iso)
        _send_confirm_buttons(account, wa_id, consultant, scheduled_at)
        server.set_chat_booking_state(chat_id, {"step": "confirming", "consultant_id": consultant["id"], "scheduled_at": iso, "urgent": urgent})
        return True

    if step == "confirming":
        if selected_id in ("booking_confirm_yes", "booking_confirm_no"):
            confirmed = selected_id == "booking_confirm_yes"
        else:
            confirmed = parse_yes_no(text)
            if confirmed is None:
                _send_text(account, phone, "Não entendi. Responda SIM ou NÃO (ou toque num dos botões).")
                return True  # mantém o estado — dá outra chance em vez de cancelar
        server.set_chat_booking_state(chat_id, None)
        if not confirmed:
            _send_text(account, phone, "Agendamento cancelado. Digite \"agendar\" pra começar de novo.")
            return True
        consultant = server.get_consultant(state["consultant_id"])
        if not consultant:
            _send_text(account, phone, f"{_term(account)} não encontrado. Digite \"agendar\" pra começar de novo.")
            return True
        scheduled_at = datetime.datetime.fromisoformat(state["scheduled_at"])
        if not book_appointment(consultant, contact_id, wa_id, push_name, scheduled_at, notify_consultant=True, requires_confirmation=True):
            _send_text(account, phone, "Esse horário acabou de ser ocupado por outra pessoa. Digite \"agendar\" pra escolher outro.")
        return True

    server.set_chat_booking_state(chat_id, None)
    return False


def _retry_or_give_up(account, chat_id, wa_id, phone, state):
    """Chamado quando a resposta do passo atual (choosing_consultant ou
    choosing_slot) não foi reconhecida. Em vez de cancelar o fluxo de cara
    (comportamento antigo), reenvia a lista até 3 tentativas — só desiste
    depois disso, sem avisar ninguém da clínica (decisão do usuário)."""
    import server

    attempts = state.get("attempts", 0) + 1
    if attempts >= 3:
        server.set_chat_booking_state(chat_id, None)
        _send_text(account, phone, "Vamos com calma — digite \"agendar\" quando puder tentar de novo.")
        return
    new_state = _resend_current_step(account, wa_id, state)
    if new_state is None:
        server.set_chat_booking_state(chat_id, None)
        _send_text(account, phone, f"{_term(account)} não encontrado. Digite \"agendar\" pra começar de novo.")
        return
    new_state["attempts"] = attempts
    server.set_chat_booking_state(chat_id, new_state)


def _resend_current_step(account, wa_id, state):
    """Reenvia a lista/pergunta do passo atual sem perder o progresso —
    usado tanto pelo comando "ajuda" quanto pelo retry de entrada inválida.
    Devolve o novo booking_state (pode trazer options recalculadas, ex:
    horários que já não estão mais livres) ou None se o consultor sumiu."""
    import server

    step = state.get("step")
    urgent = bool(state.get("urgent"))
    if step == "choosing_consultant":
        consultants = []
        for cid in state.get("options", []):
            c = server.get_consultant(cid)
            if c:
                consultants.append(c)
        if not consultants:
            return None
        option_ids = _send_consultant_list(account, wa_id, consultants, urgent=urgent, reminder=True)
        return {"step": "choosing_consultant", "options": option_ids, "urgent": urgent}
    if step == "choosing_slot":
        consultant = server.get_consultant(state.get("consultant_id"))
        if not consultant:
            return None
        slots = compute_free_slots(consultant, limit=2 if urgent else 10)
        option_isos = _send_slot_list(account, wa_id, consultant, slots, reminder=True)
        return {"step": "choosing_slot", "consultant_id": consultant["id"], "options": option_isos, "urgent": urgent}
    if step == "confirming":
        consultant = server.get_consultant(state.get("consultant_id"))
        if not consultant:
            return None
        scheduled_at = datetime.datetime.fromisoformat(state["scheduled_at"])
        _send_confirm_buttons(account, wa_id, consultant, scheduled_at)
        return state
    return None


def _send_text(account, phone, text):
    try:
        evolution.send_text(account["wa_session_name"], phone, text)
    except EvolutionError:
        pass


def _send_consultant_list(account, wa_id, consultants, urgent=False, reminder=False):
    """Texto puro, não lista interativa (send_list) — a Evolution API/Baileys
    desta versão quebra ao montar o listMessage ('TypeError: this.isZero is
    not a function', erro de baixo nível na serialização do protobuf, dentro
    do próprio Baileys) mesmo com um payload válido — a mensagem nem chega a
    sair. Mesma decisão já tomada pro convite de consultor (send_buttons →
    send_text): só o texto numerado, que já era o fallback pra quem não
    conseguia tocar na lista, agora é o único caminho. Devolve os IDs na
    mesma ordem mostrada, pra handle_incoming resolver a resposta numérica
    guardada em booking_state["options"].

    Mostra a especialidade (campo `context` do consultor) junto do nome,
    quando cadastrada — ajuda a escolher certo sem precisar adivinhar,
    principalmente quem está com pressa."""
    consultants = consultants[:10]
    lines = [
        f"{i+1}. {c['name']}" + (f" — {c['context']}" if c.get("context") else "")
        for i, c in enumerate(consultants)
    ]
    numbered = "\n".join(lines)
    term = _term(account).lower()
    if reminder:
        intro = "Não consegui entender 🙏 Aqui está a lista de novo, é só digitar o número:"
    elif urgent:
        intro = f"Vamos te encaixar o quanto antes. Escolha um {term}, digite o número:"
    else:
        intro = f"Escolha um {term}, digite o número:"
    parts = []
    if urgent:
        parts.append(
            "⚠️ Se isso for uma emergência grave (dor forte no peito, falta de ar, "
            "sangramento intenso, desmaio), ligue 192 (SAMU) ou vá direto ao "
            "pronto-socorro mais próximo."
        )
    parts.append(intro)
    parts.append(numbered)
    try:
        evolution.send_text(account["wa_session_name"], _phone(wa_id), "\n\n".join(parts))
    except EvolutionError:
        pass
    return [c["id"] for c in consultants]


def _send_slot_list(account, wa_id, consultant, slots, reminder=False):
    """Mesma lógica de _send_consultant_list — devolve os horários (ISO) na
    ordem mostrada. Dia da semana sempre em português (WEEKDAY_NAMES_PT), pra
    não depender do locale configurado no processo do servidor (%a do
    strftime sairia em inglês se o locale não for pt_BR)."""
    lines = [
        f"{i+1}. {WEEKDAY_NAMES_PT[s.weekday()]} {s.strftime('%d/%m %H:%M')}"
        for i, s in enumerate(slots)
    ]
    numbered = "\n".join(lines)
    intro = ("Não consegui entender 🙏 Aqui está a lista de novo, é só digitar o número:" if reminder
             else f"Escolha um horário com {consultant['name']}, digite o número:")
    try:
        evolution.send_text(account["wa_session_name"], _phone(wa_id), f"{intro}\n\n{numbered}")
    except EvolutionError:
        pass
    return [s.isoformat() for s in slots]


def _send_confirm_buttons(account, wa_id, consultant, scheduled_at):
    when = scheduled_at.strftime("%d/%m às %H:%M")
    try:
        evolution.send_buttons(
            account["wa_session_name"], _phone(wa_id), "Confirmar agendamento",
            f"Confirma agendamento com {consultant['name']} em {when}? "
            f"Toque num botão acima ou responda SIM ou NÃO.",
            [{"id": "booking_confirm_yes", "text": "Sim, confirmar"}, {"id": "booking_confirm_no", "text": "Não"}],
        )
    except EvolutionError:
        pass
