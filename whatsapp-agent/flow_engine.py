"""Construtor de fluxo de atendimento automático — conversa configurável
pelo dono da conta (contas em modo Consultores), guardada em
whatsapp_chats.flow_state. Chamado pelo webhook em server.py
(webhook_evolution), no mesmo espírito de booking_flow.py — aliás,
reaproveita boa parte dele: tolerância a erro de digitação via
text_menu.py, e o próprio motor de agendamento (booking_flow.
start_from_external) quando uma etapa dispara "Iniciar agendamento", pra
não duplicar a lógica de lista de profissionais/horários/confirmação já
validada ali.

Diferente de booking_flow.py (uma máquina de estados FIXA, sempre na mesma
ordem), aqui os passos e as ramificações vêm do banco
(whatsapp_flows/whatsapp_flow_steps), configurados pelo dono da conta na
Área do Cliente, aba "Fluxos".

Os dois motores se alternam na mesma conversa por colunas SEPARADAS
(flow_state aqui, booking_state em booking_flow.py): quando uma etapa
dispara "Iniciar agendamento", este módulo limpa flow_state e entrega o
controle pro booking_flow (que passa a usar booking_state). A conversa não
volta sozinha pro fluxo configurável depois — só se uma mensagem nova bater
de novo em algum gatilho.

Nunca deixa o paciente "preso": qualquer situação sem saída clara (etapa
apagada, opção de menu quebrada, erro de digitação repetido) cai em
"falar com atendente" em vez de ficar em silêncio — diferente de
booking_flow, que hoje desiste em silêncio depois de 3 tentativas.

Import tardio de server (mesmo motivo do booking_flow.py: server.py importa
este módulo, um "import server" no topo daqui criaria ciclo)."""

import booking_flow
from connectors.evolution import EvolutionError
import connectors.evolution as evolution
from text_menu import parse_index, matches_any

BACK_WORDS = ("voltar", "volta")
MENU_WORDS = ("menu", "inicio", "início")
ATTENDANT_WORDS = ("atendente", "humano", "pessoa")
MAX_ATTEMPTS = 2
MAX_AUTO_HOPS = 20  # trava de segurança contra ciclo de etapas "message" sem fim


def _phone(wa_id):
    return (wa_id or "").split("@")[0]


def _send_text(account, phone, text):
    if not text:
        return
    try:
        evolution.send_text(account["wa_session_name"], phone, text)
        import server
        server.report_whatsapp_sent_usage(account["id"])
    except EvolutionError:
        pass


def _render(template, ctx):
    import server
    return server.render_message_template(template or "", ctx)


def _ctx(account, push_name, vars_):
    import server
    nomenclature = server.get_nomenclature(account.get("user_id"))
    ctx = {
        "cliente_nome": push_name or "",
        "clinica": account.get("label") or "nossa clínica",
        "consultor_termo": nomenclature["consultant"]["singular"],
    }
    ctx.update(vars_ or {})
    return ctx


def _find_step(steps, step_key):
    return next((s for s in steps if s["step_key"] == step_key), None)


def _find_root(steps):
    return next((s for s in steps if s.get("is_root")), None)


def _match_flow(flows, text):
    """Primeiro fluxo cujas palavras-gatilho batem com o texto (ordem de
    sort_order = prioridade); se nenhum bater, o fluxo marcado como padrão
    (se houver); senão None (webhook segue pro próximo candidato)."""
    default = None
    for f in flows:
        if f.get("is_default") and default is None:
            default = f
        keywords = tuple(f.get("trigger_keywords") or [])
        if keywords and matches_any(text, words=keywords):
            return f
    return default


def handle_incoming(account, chat_id, contact_id, wa_id, text, selected_id, push_name=None):
    """Ponto de entrada chamado pelo webhook — mesmo contrato de
    booking_flow.handle_incoming(...). Retorna True se a mensagem foi
    absorvida por um fluxo configurável, False se a conta não tem nenhum
    fluxo ativo ou nenhum casou com o texto (webhook segue pro próximo
    candidato: booking_flow ou o fallback de IA)."""
    import server  # tardio — ver docstring do módulo

    if server.plan_booking_mode(account.get("user_id")) != "consultores":
        if server.get_chat_flow_state(chat_id) is not None:
            server.set_chat_flow_state(chat_id, None)
        return False

    phone = _phone(wa_id)
    state = server.get_chat_flow_state(chat_id)

    if state is None:
        flows = server.get_flows(account["id"])
        if not flows:
            return False
        flow = _match_flow(flows, text)
        if not flow:
            return False
        steps = server.get_flow_steps(flow["id"])
        root = _find_root(steps)
        if not root:
            return False
        _enter_step(account, chat_id, wa_id, push_name, flow["id"], steps, root, {}, [])
        return True

    flows = server.get_flows(account["id"])
    flow = next((f for f in flows if f["id"] == state.get("flow_id")), None)
    if not flow:
        server.set_chat_flow_state(chat_id, None)
        return False
    steps = server.get_flow_steps(flow["id"])

    # Comandos globais — funcionam em qualquer etapa, só pra resposta
    # digitada (nunca confundidos com um clique de botão/lista).
    if not selected_id and matches_any(text, words=ATTENDANT_WORDS):
        _run_action(account, chat_id, wa_id, push_name, state.get("vars") or {}, "human_handoff", None)
        return True
    if not selected_id and matches_any(text, words=MENU_WORDS):
        root = _find_root(steps)
        if root:
            _enter_step(account, chat_id, wa_id, push_name, flow["id"], steps, root, state.get("vars") or {}, [])
        else:
            server.set_chat_flow_state(chat_id, None)
        return True
    if not selected_id and matches_any(text, words=BACK_WORDS):
        history = list(state.get("history") or [])
        prev_step = None
        while history and prev_step is None:
            prev_step = _find_step(steps, history.pop())
        if prev_step:
            _enter_step(account, chat_id, wa_id, push_name, flow["id"], steps, prev_step, state.get("vars") or {}, history)
        else:
            _send_text(account, phone, "Você já está na primeira etapa.")
            _resend_current(account, wa_id, push_name, state, steps)
        return True

    step = _find_step(steps, state.get("step"))
    if not step:
        # Etapa referenciada sumiu (ex: apagada depois que a conversa já
        # tinha chegado nela) — nunca trava o paciente numa etapa fantasma.
        _run_action(account, chat_id, wa_id, push_name, state.get("vars") or {}, "human_handoff", None)
        return True

    if step["step_type"] == "menu":
        options = step.get("options") or []
        idx = None
        if selected_id and selected_id.startswith("flowopt_"):
            try:
                idx = int(selected_id.split("_", 1)[1])
            except (ValueError, IndexError):
                idx = None
        if idx is None:
            labels = [o.get("label", "") for o in options]
            idx = parse_index(text, options, names=labels)
        if idx is None or not (0 <= idx < len(options)):
            _retry_or_handoff(account, chat_id, wa_id, push_name, step, state)
            return True
        chosen = options[idx]
        history = list(state.get("history") or []) + [step["step_key"]]
        _advance(account, chat_id, wa_id, push_name, flow["id"], steps, state.get("vars") or {},
                  history, chosen.get("goto_step_key"), chosen.get("goto_action"))
        return True

    if step["step_type"] == "collect_input":
        value = (text or "").strip()
        if not value:
            _retry_or_handoff(account, chat_id, wa_id, push_name, step, state)
            return True
        vars_ = dict(state.get("vars") or {})
        if step.get("variable_name"):
            vars_[step["variable_name"]] = value
        history = list(state.get("history") or []) + [step["step_key"]]
        _advance(account, chat_id, wa_id, push_name, flow["id"], steps, vars_, history, step.get("next_step_key"), None)
        return True

    # message/action nunca ficam "esperando" resposta (avançam sozinhos ao
    # serem exibidos) — chegar aqui significa flow_state ficou pendurado
    # numa etapa que não deveria pausar. Encaminha pra atendente em vez de
    # repetir em loop.
    _run_action(account, chat_id, wa_id, push_name, state.get("vars") or {}, "human_handoff", None)
    return True


def _resend_current(account, wa_id, push_name, state, steps):
    step = _find_step(steps, state.get("step"))
    if step:
        _send_step_prompt(account, wa_id, push_name, step, state.get("vars") or {})


def _retry_or_handoff(account, chat_id, wa_id, push_name, step, state):
    import server
    attempts = int(state.get("attempts") or 0) + 1
    if attempts > MAX_ATTEMPTS:
        _run_action(account, chat_id, wa_id, push_name, state.get("vars") or {}, "human_handoff", None)
        return
    _send_text(account, _phone(wa_id), "Não entendi 🙏")
    _send_step_prompt(account, wa_id, push_name, step, state.get("vars") or {})
    new_state = dict(state)
    new_state["attempts"] = attempts
    server.set_chat_flow_state(chat_id, new_state)


def _advance(account, chat_id, wa_id, push_name, flow_id, steps, vars_, history, goto_step_key, goto_action):
    """Resolve o destino de uma opção de menu ou do next_step_key de uma
    etapa collect_input: ou é outra etapa do fluxo, ou uma das 4 ações
    fixas, ou nenhum dos dois — fim implícito (válido: collect_input sem
    next_step_key configurado, "essa pergunta é a última do fluxo"), não
    um erro. Só cai em atendente quando um destino FOI configurado mas não
    existe mais (dado inconsistente — não deveria acontecer, a rota de
    salvar já valida isso — mas nunca trava o paciente se acontecer)."""
    import server
    if not goto_step_key and not goto_action:
        server.set_chat_flow_state(chat_id, None)
        return
    if goto_step_key:
        target = _find_step(steps, goto_step_key)
        if target:
            _enter_step(account, chat_id, wa_id, push_name, flow_id, steps, target, vars_, history)
            return
    if goto_action in ("start_booking", "human_handoff", "faq_ai", "end"):
        _run_action(account, chat_id, wa_id, push_name, vars_, goto_action, None)
        return
    server.set_chat_flow_state(chat_id, None)
    _run_action(account, chat_id, wa_id, push_name, vars_, "human_handoff", None)


def _enter_step(account, chat_id, wa_id, push_name, flow_id, steps, step, vars_, history, _hops=0):
    """Exibe uma etapa. message/action avançam sozinhos (sem esperar
    resposta do paciente) — message encadeia direto pro next_step_key
    (com trava de segurança contra ciclo infinito); action dispara na hora.
    menu/collect_input mandam a etapa e PAUSAM (persistem flow_state
    esperando a resposta do paciente)."""
    import server
    ctx = _ctx(account, push_name, vars_)
    phone = _phone(wa_id)

    if step["step_type"] == "action":
        _run_action(account, chat_id, wa_id, push_name, vars_, step.get("action_type"), step.get("message_template"))
        return

    if step["step_type"] == "message":
        _send_text(account, phone, _render(step.get("message_template"), ctx))
        next_key = step.get("next_step_key")
        if not next_key:
            server.set_chat_flow_state(chat_id, None)
            return
        next_step = _find_step(steps, next_key)
        if not next_step or _hops >= MAX_AUTO_HOPS:
            _run_action(account, chat_id, wa_id, push_name, vars_, "human_handoff", None)
            return
        _enter_step(account, chat_id, wa_id, push_name, flow_id, steps, next_step, vars_, history, _hops=_hops + 1)
        return

    # menu / collect_input — manda a etapa e persiste, esperando resposta.
    _send_step_prompt(account, wa_id, push_name, step, vars_)
    server.set_chat_flow_state(chat_id, {
        "flow_id": flow_id, "step": step["step_key"], "history": history, "vars": vars_, "attempts": 0,
    })


def _send_step_prompt(account, wa_id, push_name, step, vars_):
    ctx = _ctx(account, push_name, vars_)
    phone = _phone(wa_id)
    intro = _render(step.get("message_template"), ctx)

    if step["step_type"] == "collect_input":
        _send_text(account, phone, intro)
        return

    # menu: lista numerada em texto (send_list quebra nesta versão da
    # Evolution API/Baileys pra >2 opções — mesma decisão já tomada em
    # booking_flow._send_consultant_list). Com exatamente 2 opções, manda
    # também botões nativos como reforço (mesmo padrão da confirmação
    # sim/não do agendamento) — se falhar, o texto numerado já foi.
    options = step.get("options") or []
    lines = [f"{i + 1}. {o.get('label', '')}" for i, o in enumerate(options)]
    _send_text(account, phone, f"{intro}\n\n" + "\n".join(lines))
    if len(options) == 2:
        try:
            evolution.send_buttons(
                account["wa_session_name"], phone, "Escolha uma opção", intro,
                [{"id": f"flowopt_{i}", "text": (o.get("label") or "")[:24]} for i, o in enumerate(options)],
            )
        except EvolutionError:
            pass


def _run_action(account, chat_id, wa_id, push_name, vars_, action_type, message_template):
    import server
    phone = _phone(wa_id)
    ctx = _ctx(account, push_name, vars_)
    if message_template:
        _send_text(account, phone, _render(message_template, ctx))

    if action_type == "start_booking":
        server.set_chat_flow_state(chat_id, None)
        subject = "; ".join(str(v) for v in vars_.values()) if vars_ else None
        if not booking_flow.start_from_external(account, chat_id, wa_id, subject=subject):
            _send_text(account, phone, f"Não há {server.get_nomenclature(account.get('user_id'))['consultant']['singular'].lower()}es disponíveis no momento. Tente de novo mais tarde.")
        return

    if action_type == "human_handoff":
        server.set_chat_flow_state(chat_id, None)
        server.set_chat_auto_reply(chat_id, False)
        if not message_template:
            _send_text(account, phone, "Vou te colocar em contato com nossa equipe, já te respondemos por aqui.")
        full_account = server.get_account(account["id"])
        secretary_wa_id = full_account.get("secretary_wa_id") if full_account else None
        if secretary_wa_id:
            try:
                evolution.send_text(
                    account["wa_session_name"], server._phone_from_wa_id(secretary_wa_id),
                    f"🔔 Um paciente pediu atendimento humano: {push_name or phone} ({phone}).",
                )
                server.report_whatsapp_sent_usage(account["id"])
            except EvolutionError as e:
                server.log_event(account["id"], "flow_handoff_notify_failed", level="warning", detail={"error": str(e)})
        else:
            server.log_event(account["id"], "flow_handoff_no_secretary_contact", level="warning", detail={"chat_id": chat_id})
        return

    if action_type == "faq_ai":
        server.set_chat_flow_state(chat_id, None)
        return

    # "end" (ou action_type ausente/inválido — defesa em profundidade)
    server.set_chat_flow_state(chat_id, None)
