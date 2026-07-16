"""Cliente REST da Evolution API — conector de baixo nível pras contas
'qrcode' (WhatsApp comum, via Baileys). Roda bare-metal (Node.js, sem
Docker) no próprio servidor — ver evolution-api/ na raiz do monorepo e
evolution-api.service no systemd.

Formato confirmado lendo o código-fonte da Evolution API (v2.3.7) direto —
não só a documentação — e testando contra a instância real rodando:
POST /instance/create já devolve o QR na resposta (sem precisar de polling
como no conector antigo); GET /instance/connectionState/{name} retorna
state 'close' | 'connecting' | 'open'; POST /message/sendText/{name} espera
{"number", "text"}; webhook é configurado à parte via POST /webhook/set/{name}.

Este módulo só fala HTTP com a Evolution API — nenhuma regra de negócio ou
acesso a banco aqui (isso fica em server.py).
"""

import requests

from config import EVOLUTION_CONFIG

BASE_URL = (EVOLUTION_CONFIG.get("base_url") or "http://127.0.0.1:8090").rstrip("/")
API_KEY = EVOLUTION_CONFIG.get("api_key")

TIMEOUT = 15


class EvolutionError(Exception):
    pass


def _headers():
    if not API_KEY:
        raise EvolutionError(
            "evolution_api.api_key não configurado em config.yaml — "
            "veja evolution-api/.env (AUTHENTICATION_API_KEY)"
        )
    return {"apikey": API_KEY, "Content-Type": "application/json"}


def _request(method, path, expected_statuses=(200, 201), **kwargs):
    url = f"{BASE_URL}{path}"
    try:
        res = requests.request(method, url, headers=_headers(), timeout=TIMEOUT, **kwargs)
    except requests.exceptions.ConnectionError as e:
        raise EvolutionError(
            f"Não foi possível conectar à Evolution API em {BASE_URL} — "
            f"o serviço está rodando? ({e})"
        )
    except requests.exceptions.Timeout:
        raise EvolutionError(f"Evolution API não respondeu em {TIMEOUT}s ({url})")
    if res.status_code not in expected_statuses:
        raise EvolutionError(f"Evolution API retornou {res.status_code}: {res.text[:300]}")
    return res.json() if res.content else {}


def create_instance(instance_name):
    """Cria a instância — a resposta já traz o QR (chave 'qrcode'), sem
    precisar de um passo de polling separado como no conector antigo."""
    try:
        return _request(
            "POST",
            "/instance/create",
            json={"instanceName": instance_name, "qrcode": True, "integration": "WHATSAPP-BAILEYS"},
        )
    except EvolutionError as e:
        if "already" in str(e).lower() or "409" in str(e):
            return {"instance": {"instanceName": instance_name}, "already_exists": True}
        raise


def get_qr(instance_name):
    """Regenera/busca o QR de uma instância já criada (reconectar depois de
    logout, por exemplo). Retorna {'base64': 'data:image/png;base64,...'}."""
    return _request("GET", f"/instance/connect/{instance_name}")


def set_webhook(instance_name, webhook_url):
    return _request(
        "POST",
        f"/webhook/set/{instance_name}",
        json={
            "webhook": {
                "enabled": True,
                "url": webhook_url,
                "byEvents": False,
                "events": ["MESSAGES_UPSERT", "CONNECTION_UPDATE"],
            }
        },
    )


def connection_state(instance_name):
    """Estado atual: 'close' | 'connecting' | 'open' (conectado)."""
    data = _request("GET", f"/instance/connectionState/{instance_name}")
    return (data.get("instance") or {}).get("state", "close")


def send_text(instance_name, number, text):
    return _request(
        "POST",
        f"/message/sendText/{instance_name}",
        json={"number": number, "text": text},
    )


def send_buttons(instance_name, number, title, description, buttons, footer=None):
    """buttons: [{"id": "...", "text": "..."}] — vira botões do tipo 'reply'
    (o único tipo que faz sentido pra uma resposta de sim/não/escolha; a
    Evolution API também suporta 'copy'/'url'/'call'/'pix', não usados aqui).
    Shape confirmado lendo evolution-api/src/api/dto/sendMessage.dto.ts
    (classes Metadata/Button/SendButtonsDto) direto no servidor.
    `footer` só entra no payload quando tem valor — o DTO valida como
    string opcional, e null explícito (o que aconteceria mandando a chave
    sempre) é rejeitado pela validação ('footer is not of a type(s) string')."""
    payload = {
        "number": number,
        "title": title,
        "description": description,
        "buttons": [{"type": "reply", "displayText": b["text"], "id": b["id"]} for b in buttons],
    }
    if footer:
        payload["footer"] = footer
    return _request("POST", f"/message/sendButtons/{instance_name}", json=payload)


def send_list(instance_name, number, title, description, button_text, sections, footer=None):
    """sections: [{"title": "...", "rows": [{"id": "...", "title": "...", "description": "..."}]}].
    WhatsApp limita a 10 linhas por lista, no total — quem chama precisa
    respeitar esse limite. Shape confirmado em sendMessage.dto.ts (SendListDto/
    Section/Row) direto no servidor. `footerText` só entra no payload quando
    tem valor — mesmo motivo de send_buttons acima."""
    payload = {
        "number": number,
        "title": title,
        "description": description,
        "buttonText": button_text,
        "sections": [
            {
                "title": s["title"],
                "rows": [{"rowId": r["id"], "title": r["title"], "description": r.get("description", "")} for r in s["rows"]],
            }
            for s in sections
        ],
    }
    if footer:
        payload["footerText"] = footer
    return _request("POST", f"/message/sendList/{instance_name}", json=payload)


def logout(instance_name):
    return _request("DELETE", f"/instance/logout/{instance_name}", expected_statuses=(200, 201, 404))


def delete_instance(instance_name):
    return _request("DELETE", f"/instance/delete/{instance_name}", expected_statuses=(200, 201, 404))
