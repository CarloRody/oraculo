"""Cliente REST da WAHA (WhatsApp HTTP API) — conector de baixo nível pras
contas 'qrcode' (WhatsApp comum). Roda em Docker no próprio servidor
(ver whatsapp-agent/docker/docker-compose.yml), exposto só em 127.0.0.1.

Formato confirmado testando direto contra o container (WAHA 2026.6.2,
engine WEBJS): POST /api/sessions cria+inicia; GET /api/sessions/{name}
retorna status (STARTING -> SCAN_QR_CODE -> WORKING); GET /api/{name}/auth/qr
só funciona com status=SCAN_QR_CODE, senão devolve 422.

Este módulo só fala HTTP com a WAHA — nenhuma regra de negócio ou acesso a
banco aqui (isso fica em server.py).
"""

import requests

from config import EVOLUTION_CONFIG as WAHA_CONFIG

BASE_URL = (WAHA_CONFIG.get("base_url") or "http://127.0.0.1:8080").rstrip("/")
API_KEY = WAHA_CONFIG.get("api_key")

TIMEOUT = 15


class WahaError(Exception):
    pass


def _headers():
    if not API_KEY:
        raise WahaError(
            "evolution_api.api_key não configurado em config.yaml — "
            "veja whatsapp-agent/docker/.env"
        )
    return {"X-Api-Key": API_KEY, "Content-Type": "application/json"}


def _request(method, path, expected_statuses=(200, 201), **kwargs):
    url = f"{BASE_URL}{path}"
    try:
        res = requests.request(method, url, headers=_headers(), timeout=TIMEOUT, **kwargs)
    except requests.exceptions.ConnectionError as e:
        raise WahaError(
            f"Não foi possível conectar à WAHA em {BASE_URL} — o container está rodando? ({e})"
        )
    except requests.exceptions.Timeout:
        raise WahaError(f"WAHA não respondeu em {TIMEOUT}s ({url})")
    if res.status_code not in expected_statuses:
        raise WahaError(f"WAHA retornou {res.status_code}: {res.text[:300]}")
    return res.json() if res.content else {}


def create_and_start_session(session_name):
    """Cria e inicia a sessão na primeira vez; se já existir (409, reconectar
    uma conta que já tinha sido criada antes), reinicia com /start."""
    try:
        return _request(
            "POST", "/api/sessions", json={"name": session_name, "start": True}
        )
    except WahaError as e:
        if "409" in str(e):
            return _request("POST", f"/api/sessions/{session_name}/start", expected_statuses=(200, 201))
        raise


def session_status(session_name):
    """Status atual: STARTING | SCAN_QR_CODE | WORKING | FAILED | STOPPED."""
    data = _request("GET", f"/api/sessions/{session_name}")
    return data.get("status", "STOPPED")


def get_qr(session_name):
    """QR pra escanear — só funciona quando o status é SCAN_QR_CODE.
    Retorna {'mimetype': 'image/png', 'data': '<base64 sem prefixo data:>'}."""
    url = f"{BASE_URL}/api/{session_name}/auth/qr"
    headers = _headers()
    headers["Accept"] = "application/json"
    try:
        res = requests.get(url, headers=headers, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        raise WahaError(f"Não foi possível conectar à WAHA em {BASE_URL} ({e})")
    if res.status_code == 422:
        raise WahaError("Sessão ainda não está pronta pra gerar QR (tente de novo em alguns segundos)")
    if res.status_code != 200:
        raise WahaError(f"WAHA retornou {res.status_code}: {res.text[:300]}")
    return res.json()


def stop_session(session_name):
    return _request("POST", f"/api/sessions/{session_name}/stop", expected_statuses=(200, 201, 404))


def delete_session(session_name):
    return _request("DELETE", f"/api/sessions/{session_name}", expected_statuses=(200, 404))
