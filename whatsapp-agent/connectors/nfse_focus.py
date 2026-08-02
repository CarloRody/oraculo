"""Cliente REST da Focus NFe — emissão de NFS-e no Ambiente Nacional
(Portal Nacional da NFS-e / SNNFSE), usado pela clínica de Divinópolis-MG
(ver nfse_flow.py pra regra de negócio e fila; config.yaml, seção `nfse`).

Autenticação confirmada na doc oficial (doc.focusnfe.com.br/reference/
autenticacao): HTTP Basic, token da empresa (gerado no painel da Focus
NFe, um por ambiente — homologação/produção) como usuário, senha em
branco. Mesmo esquema pros dois ambientes, só muda o token e a base_url
usada (homologação e produção respondem no mesmo host,
api.focusnfe.com.br — o ambiente é decidido pelo TOKEN configurado, não
pela URL).

Endpoint de emissão no Ambiente Nacional: POST /v2/nfsen?ref=<referencia>
— `referencia` é uma chave de idempotência escolhida por quem chama (aqui,
usamos o id da linha de whatsapp_invoices), não gerada pela Focus NFe.
Consulta e cancelamento usam essa mesma referência. Payload exato
(prestador/tomador/serviço) e nome dos campos de resposta a confirmar
contra https://doc.focusnfe.com.br/reference/nfse-nacional e o guia
específico de Divinópolis (focusnfe.com.br/blog/como-emitir-nota-fiscal-
de-servico-nfse-em-divinopolis-mg/) antes de emitir a primeira nota real
— o formato abaixo é o padrão documentado pela Focus NFe pros outros
documentos fiscais (NFS-e municipal, NF-e), que eles mantêm consistente
entre APIs.

Este módulo só fala HTTP com a Focus NFe — nenhuma regra de negócio ou
acesso a banco aqui (isso fica em nfse_flow.py).
"""

import requests

from config import NFSE_CONFIG

BASE_URL = (NFSE_CONFIG.get("base_url") or "https://api.focusnfe.com.br").rstrip("/")
TOKEN = NFSE_CONFIG.get("token")

TIMEOUT = 30


class NfseError(Exception):
    pass


def _auth():
    if not TOKEN:
        raise NfseError(
            "nfse.token não configurado em config.yaml — gere o token da "
            "empresa no painel da Focus NFe (homologação ou produção)"
        )
    return (TOKEN, "")


def _request(method, path, expected_statuses=(200, 201, 202), timeout=None, **kwargs):
    url = f"{BASE_URL}{path}"
    try:
        res = requests.request(method, url, auth=_auth(), timeout=timeout or TIMEOUT, **kwargs)
    except requests.exceptions.ConnectionError as e:
        raise NfseError(f"Não foi possível conectar à Focus NFe em {BASE_URL} ({e})") from e
    except requests.exceptions.Timeout:
        raise NfseError(f"Focus NFe não respondeu em {timeout or TIMEOUT}s ({url})")
    if res.status_code not in expected_statuses:
        raise NfseError(f"Focus NFe retornou {res.status_code}: {res.text[:500]}")
    return res.json() if res.content else {}


def emit_invoice(referencia, payload):
    """Envia a DPS pro Ambiente Nacional — resposta imediata só confirma o
    recebimento (status 'processando_autorizacao'), a autorização real
    chega depois (ver get_invoice_status, chamado pelo polling de
    nfse_flow.nfse_loop). `referencia` é a chave de idempotência (aqui,
    o id de whatsapp_invoices) — reenviar a mesma referência não duplica
    a nota."""
    return _request("POST", f"/v2/nfsen?ref={referencia}", json=payload)


def get_invoice_status(referencia):
    """Consulta o status atual de uma nota já enviada, pela mesma
    `referencia` usada em emit_invoice."""
    return _request("GET", f"/v2/nfsen/{referencia}")


def cancel_invoice(referencia, justificativa):
    return _request("DELETE", f"/v2/nfsen/{referencia}", json={"justificativa": justificativa})
