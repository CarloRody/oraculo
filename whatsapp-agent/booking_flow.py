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


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def _phone(wa_id):
    return (wa_id or "").split("@")[0]


def _is_trigger(text):
    t = (text or "").strip().lower()
    return any(w in t for w in TRIGGER_WORDS)


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


def _create_appointment_if_free(consultant, client_contact_id, scheduled_at):
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
            """INSERT INTO whatsapp_appointments (consultant_id, client_contact_id, scheduled_at, duration_minutes)
               VALUES (%s, %s, %s, %s)""",
            (consultant["id"], client_contact_id, scheduled_at, duration),
        )
        conn.commit()
        return True
    finally:
        conn.close()


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
        _send_consultant_list(account, wa_id, consultants)
        server.set_chat_booking_state(chat_id, {"step": "choosing_consultant"})
        return True

    step = state.get("step")
    phone = _phone(wa_id)

    if step == "choosing_consultant":
        if not selected_id or not selected_id.startswith("consultant_"):
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, "Não entendi. Digite \"agendar\" pra começar de novo.")
            return True
        consultant_id = int(selected_id.split("_")[1])
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
        _send_slot_list(account, wa_id, consultant, slots)
        server.set_chat_booking_state(chat_id, {"step": "choosing_slot", "consultant_id": consultant_id})
        return True

    if step == "choosing_slot":
        if not selected_id or not selected_id.startswith("slot_"):
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, "Não entendi. Digite \"agendar\" pra começar de novo.")
            return True
        _, consultant_id_str, iso = selected_id.split("_", 2)
        consultant = server.get_consultant(int(consultant_id_str))
        if not consultant:
            server.set_chat_booking_state(chat_id, None)
            _send_text(account, phone, "Consultor não encontrado. Digite \"agendar\" pra começar de novo.")
            return True
        scheduled_at = datetime.datetime.fromisoformat(iso)
        _send_confirm_buttons(account, wa_id, consultant, scheduled_at)
        server.set_chat_booking_state(chat_id, {"step": "confirming", "consultant_id": consultant["id"], "scheduled_at": iso})
        return True

    if step == "confirming":
        server.set_chat_booking_state(chat_id, None)
        if selected_id != "booking_confirm_yes":
            _send_text(account, phone, "Agendamento cancelado. Digite \"agendar\" pra começar de novo.")
            return True
        consultant = server.get_consultant(state["consultant_id"])
        if not consultant:
            _send_text(account, phone, "Consultor não encontrado. Digite \"agendar\" pra começar de novo.")
            return True
        scheduled_at = datetime.datetime.fromisoformat(state["scheduled_at"])
        if not _create_appointment_if_free(consultant, contact_id, scheduled_at):
            _send_text(account, phone, "Esse horário acabou de ser ocupado por outra pessoa. Digite \"agendar\" pra escolher outro.")
            return True
        when = scheduled_at.strftime("%d/%m às %H:%M")
        _send_text(account, phone, f"Agendamento confirmado com {consultant['name']} em {when}!")
        client_label = push_name or phone
        try:
            evolution.send_text(consultant["wa_session_name"], _phone(consultant["wa_id"]),
                                 f"Novo agendamento: {client_label} em {when}.")
        except EvolutionError:
            pass
        return True

    server.set_chat_booking_state(chat_id, None)
    return False


def _send_text(account, phone, text):
    try:
        evolution.send_text(account["wa_session_name"], phone, text)
    except EvolutionError:
        pass


def _send_consultant_list(account, wa_id, consultants):
    sections = [{
        "title": "Consultores disponíveis",
        "rows": [{"id": f"consultant_{c['id']}", "title": c["name"], "description": (c.get("context") or "")[:70]}
                  for c in consultants[:10]],
    }]
    try:
        evolution.send_list(account["wa_session_name"], _phone(wa_id), "Agendamento",
                             "Escolha um consultor:", "Ver consultores", sections)
    except EvolutionError:
        pass


def _send_slot_list(account, wa_id, consultant, slots):
    sections = [{
        "title": "Horários disponíveis",
        "rows": [{"id": f"slot_{consultant['id']}_{s.isoformat()}", "title": s.strftime("%a %d/%m %H:%M"), "description": ""}
                  for s in slots],
    }]
    try:
        evolution.send_list(account["wa_session_name"], _phone(wa_id), "Agendamento",
                             f"Escolha um horário com {consultant['name']}:", "Ver horários", sections)
    except EvolutionError:
        pass


def _send_confirm_buttons(account, wa_id, consultant, scheduled_at):
    when = scheduled_at.strftime("%d/%m às %H:%M")
    try:
        evolution.send_buttons(
            account["wa_session_name"], _phone(wa_id), "Confirmar agendamento",
            f"Confirma agendamento com {consultant['name']} em {when}?",
            [{"id": "booking_confirm_yes", "text": "Sim, confirmar"}, {"id": "booking_confirm_no", "text": "Não"}],
        )
    except EvolutionError:
        pass
