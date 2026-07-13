"""Cliente REST da Evolution API — conector de baixo nível pras contas
'qrcode' (WhatsApp comum via Baileys). Roda em Docker no próprio servidor
(ver whatsapp-agent/docker/docker-compose.yml), exposto só em 127.0.0.1.

Este módulo só fala HTTP com a Evolution API — nenhuma regra de negócio ou
acesso a banco aqui (isso fica em server.py).
"""

import requests

from config import EVOLUTION_CONFIG

BASE_URL = (EVOLUTION_CONFIG.get("base_url") or "http://127.0.0.1:8080").rstrip("/")
API_KEY = EVOLUTION_CONFIG.get("api_key")

TIMEOUT = 15


class EvolutionError(Exception):
    pass


def _headers():
    if not API_KEY:
        raise EvolutionError(
            "evolution_api.api_key não configurado em config.yaml — "
            "veja whatsapp-agent/docker/docker-compose.yml"
        )
    return {"apikey": API_KEY, "Content-Type": "application/json"}


def _request(method, path, **kwargs):
    url = f"{BASE_URL}{path}"
    try:
        res = requests.request(method, url, headers=_headers(), timeout=TIMEOUT, **kwargs)
    except requests.exceptions.ConnectionError as e:
        raise EvolutionError(
            f"Não foi possível conectar à Evolution API em {BASE_URL} — "
            f"o container está rodando? ({e})"
        )
    except requests.exceptions.Timeout:
        raise EvolutionError(f"Evolution API não respondeu em {TIMEOUT}s ({url})")
    if res.status_code >= 400:
        raise EvolutionError(f"Evolution API retornou {res.status_code}: {res.text[:300]}")
    return res.json() if res.content else {}


def create_instance(instance_name):
    """Cria a instância (idempotente — se já existir, a Evolution retorna 403/409
    que tratamos como 'já existe, segue o fluxo')."""
    try:
        return _request(
            "POST",
            "/instance/create",
            json={
                "instanceName": instance_name,
                "qrcode": True,
                "integration": "WHATSAPP-BAILEYS",
            },
        )
    except EvolutionError as e:
        if "already" in str(e).lower() or "409" in str(e) or "403" in str(e):
            return {"instance": {"instanceName": instance_name}, "already_exists": True}
        raise


def get_qr(instance_name):
    """Retorna o QR atual pra escanear — {'base64': 'data:image/png;base64,...'}
    (chave exata pode variar entre versões da Evolution; ver server.py)."""
    return _request("GET", f"/instance/connect/{instance_name}")


def connection_state(instance_name):
    """Estado atual da sessão: 'open' (conectado), 'connecting', 'close'."""
    data = _request("GET", f"/instance/connectionState/{instance_name}")
    return (data.get("instance") or {}).get("state", "close")


def logout(instance_name):
    return _request("DELETE", f"/instance/logout/{instance_name}")


def delete_instance(instance_name):
    return _request("DELETE", f"/instance/delete/{instance_name}")
