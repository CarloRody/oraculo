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
"""

import datetime
from zoneinfo import ZoneInfo

import psycopg2

from config import DB_CONFIG
from connectors.evolution import EvolutionError
import connectors.evolution as evolution

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
TRIGGER_WORDS = ("agendar", "marcar horário", "marcar horario")
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
YES_WORDS = {"sim", "s", "confirmar", "confirmo", "yes", "1"}
NO_WORDS = {"não", "nao", "n", "cancelar", "no", "2"}


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def _phone(wa_id):
    return (wa_id or "").split("@")[0]


def _is_trigger(text):
    t = (text or "").strip().lower()
    return any(w in t for w in TRIGGER_WORDS)


def parse_yes_no(text):
    """Fallback de texto pra quando o botão nativo do WhatsApp (viewOnceMessage/
    nativeFlowMessage, formato usado por evolution.send_buttons) não é exibido
    no aparelho do destinatário — usado tanto aqui quanto em server.py pra
    confirmação de cadastro de consultor. None = não reconheceu como sim/nem."""
    t = (text or "").strip().lower()
    if t in YES_WORDS:
        return True
    if t in NO_WORDS:
        return False
    return None


def _parse_index(text, options):
    """Fallback de texto pra resposta de lista (send_list) — 'digite o número
    da opção'. 1-based na mensagem, devolve o índice 0-based em options."""
    t = (text or "").strip()
    if not t.isdigit() or not options:
        return None
    i = int(t) - 1
    return i if 0 <= i < len(options) else None


def compute_free_slots(consultant, days_ahead=14, limit=10):
    """Próximos horários livres do consultor, cruzando weekly_availability
    (JSONB, ex: {"mon": [["09:00","12:00"]]}) com os agendamentos já
    confirmados. Retorna datetimes com timezone (America/Sao_Paulo)."""
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
               WHERE consultant_id = %s AND status = 'confirmed' AND scheduled_at >= %s""",
            (consultant["id"], now),
        )
        busy = [(r[0], r[0] + datetime.timedelta(minutes=r[1])) for r in cur.fetchall()]
    finally:
        conn.close()

    slots = []
    day = now.date()
    for _ in range(days_ahead):
        key = WEEKDAY_KEYS[day.weekday()]
        for start_str, end_str in availability.get(key, []):
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


def _create_appointment_if_free(consultant, client_contact_id, scheduled_at, subject=None):
    """pg_advisory_xact_lock serializa tentativas de agendar o MESMO
    consultor — evita duas pessoas confirmarem o mesmo horário ao mesmo
    tempo (checar disponibilidade e inserir não são atômicos sem isso)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (consultant["id"],))
        duration = consultant["slot_duration_minutes"]
        end_at = scheduled_at + datetime.timedelta(minutes=duration)
        cur.execute(
            """SELECT 1 FROM whatsapp_appointments
               WHERE consultant_id = %s AND status = 'confirmed'
               AND scheduled_at < %s AND (scheduled_at + make_interval(mins => duration_minutes)) > %s""",
            (consultant["id"], end_at, scheduled_at),
        )
        if cur.fetchone():
            conn.rollback()
            return False
        cur.execute(
            """INSERT INTO whatsapp_appointments (consultant_id, client_contact_id, scheduled_at, duration_minutes, subject)
               VALUES (%s, %s, %s, %s, %s)""",
            (consultant["id"], client_contact_id, scheduled_at, duration, subject),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def book_appointment(consultant, client_contact_id, client_wa_id, client_push_name, scheduled_at, notify_consultant=False, subject=None):
    """Cria o agendamento (se o horário ainda estiver livre) e avisa o
    cliente. Usado tanto pelo fluxo self-service do cliente (notify_consultant
    =True, ele ainda não sabe do agendamento) quanto pelo portal do próprio
    consultor (notify_consultant=False — não faz sentido avisar quem tá
    criando). `subject` é opcional — só o portal do consultor coleta isso hoje.
    Retorna True se criou, False se o horário já não estava livre."""
    if not _create_appointment_if_free(consultant, client_contact_id, scheduled_at, subject=subject):
        return False
    when = scheduled_at.strftime("%d/%m às %H:%M")
    client_phone = _phone(client_wa_id)
    subject_line = f"\nAssunto: {subject}" if subject else ""
    try:
        evolution.send_text(consultant["wa_session_name"], client_phone,
                             f"Você tem um agendamento confirmado com {consultant['name']} em {when}!{subject_line}")
    except EvolutionError:
        pass
    if notify_consultant:
        try:
            evolution.send_text(consultant["wa_session_name"], _phone(consultant["wa_id"]),
                                 f"Novo agendamento: {client_push_name or client_phone} em {when}.{subject_line}")
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
               WHERE consultant_id = %s AND status = 'confirmed' AND id != %s
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

    if state is None:
        if not _is_trigger(text):
            return False
        if not server.plan_has_agenda(account.get("user_id")):
            return False
        consultants = [c for c in server.get_consultants(account["id"]) if c["status"] == "active"]
        if not consultants:
            return False
        option_ids = _send_consultant_list(account, wa_id, consultants)
        server.set_chat_booking_state(chat_id, {"step": "choosing_consultant", "options": option_ids})
        return True

    step = state.get("step")
    phone = _phone(wa_id)

    if step == "choosing_consultant":
        consultant_id = None
        if selected_id and selected_id.startswith("consultant_"):
            consultant_id = int(selected_id.split("_")[1])
        else:
            idx = _parse_index(text, state.get("options"))
            if idx is not None:
                consultant_id = state["options"][idx]
        if consultant_id is None:
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, "Não entendi. Digite \"agendar\" pra começar de novo.")
            return True
        consultant = server.get_consultant(consultant_id)
        if not consultant or consultant["account_id"] != account["id"] or consultant["status"] != "active":
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, "Consultor não encontrado. Digite \"agendar\" pra começar de novo.")
            return True
        slots = compute_free_slots(consultant)
        if not slots:
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, f"{consultant['name']} não tem horários livres nos próximos dias. Tente de novo mais tarde.")
            return True
        option_isos = _send_slot_list(account, wa_id, consultant, slots)
        server.set_chat_booking_state(chat_id, {"step": "choosing_slot", "consultant_id": consultant_id, "options": option_isos})
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
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, "Não entendi. Digite \"agendar\" pra começar de novo.")
            return True
        consultant = server.get_consultant(state["consultant_id"])
        if not consultant:
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, "Consultor não encontrado. Digite \"agendar\" pra começar de novo.")
            return True
        scheduled_at = datetime.datetime.fromisoformat(iso)
        _send_confirm_buttons(account, wa_id, consultant, scheduled_at)
        server.set_chat_booking_state(chat_id, {"step": "confirming", "consultant_id": consultant["id"], "scheduled_at": iso})
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
            _send_text(account, phone, "Consultor não encontrado. Digite \"agendar\" pra começar de novo.")
            return True
        scheduled_at = datetime.datetime.fromisoformat(state["scheduled_at"])
        if not book_appointment(consultant, contact_id, wa_id, push_name, scheduled_at, notify_consultant=True):
            _send_text(account, phone, "Esse horário acabou de ser ocupado por outra pessoa. Digite \"agendar\" pra escolher outro.")
        return True

    server.set_chat_booking_state(chat_id, None)
    return False


def _send_text(account, phone, text):
    try:
        evolution.send_text(account["wa_session_name"], phone, text)
    except EvolutionError:
        pass


def _send_consultant_list(account, wa_id, consultants):
    """Texto puro, não lista interativa (send_list) — a Evolution API/Baileys
    desta versão quebra ao montar o listMessage ('TypeError: this.isZero is
    not a function', erro de baixo nível na serialização do protobuf, dentro
    do próprio Baileys) mesmo com um payload válido — a mensagem nem chega a
    sair. Mesma decisão já tomada pro convite de consultor (send_buttons →
    send_text): só o texto numerado, que já era o fallback pra quem não
    conseguia tocar na lista, agora é o único caminho. Devolve os IDs na
    mesma ordem mostrada, pra handle_incoming resolver a resposta numérica
    guardada em booking_state["options"]."""
    consultants = consultants[:10]
    numbered = "\n".join(f"{i+1}. {c['name']}" for i, c in enumerate(consultants))
    try:
        evolution.send_text(account["wa_session_name"], _phone(wa_id),
                             f"Escolha um consultor, digite o número:\n\n{numbered}")
    except EvolutionError:
        pass
    return [c["id"] for c in consultants]


def _send_slot_list(account, wa_id, consultant, slots):
    """Mesma lógica de _send_consultant_list — devolve os horários (ISO) na
    ordem mostrada."""
    numbered = "\n".join(f"{i+1}. {s.strftime('%a %d/%m %H:%M')}" for i, s in enumerate(slots))
    try:
        evolution.send_text(account["wa_session_name"], _phone(wa_id),
                             f"Escolha um horário com {consultant['name']}, digite o número:\n\n{numbered}")
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
