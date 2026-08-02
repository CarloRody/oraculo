"""Emissão de NFS-e (nota fiscal eletrônica de serviço) via Focus NFe —
ver connectors/nfse_focus.py pro cliente HTTP e migração #50 (db_migrations.py)
pra tabela whatsapp_invoices.

Fluxo próprio, desacoplado de whatsapp_receipts/receipts_pdf.py (decisão
explícita: o recibo existente não tem função fiscal, juntar os dois
mudaria o que "recibo" significa pra quem já usa). Mesmo espírito de
document_summary.py: fila em background (nfse_loop), reivindicação
atômica via FOR UPDATE SKIP LOCKED, cada nota despachada numa thread
própria — uma falha/demora numa nota não trava as outras nem o loop.

`import server` tardio dentro das funções que precisam de algo de lá
(get_patient_record, get_account, log_event) — mesmo motivo de
document_summary.py: evita ciclo de import (server.py importa este
módulo no topo).
"""

import threading
import time
from decimal import Decimal

import psycopg2

from config import DB_CONFIG
import connectors.nfse_focus as nfse_focus

POLL_INTERVAL_SECONDS = 15

INVOICE_COLUMNS = [
    "id", "account_id", "contact_id", "consultant_id",
    "patient_name", "patient_cpf", "patient_address",
    "service_description", "amount", "service_date",
    "status", "focus_ref", "numero_nfse", "codigo_verificacao",
    "pdf_url", "xml_url", "error_message",
    "requested_by", "requested_at", "completed_at", "cancelled_at", "cancelled_reason",
]


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def _row_to_invoice(row):
    d = dict(zip(INVOICE_COLUMNS, row))
    d["service_date"] = d["service_date"].isoformat() if d["service_date"] else None
    for k in ("requested_at", "completed_at", "cancelled_at"):
        d[k] = d[k].isoformat() if d[k] else None
    d["amount"] = float(d["amount"]) if d["amount"] is not None else None
    return d


def get_invoices(account_id, contact_id=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(INVOICE_COLUMNS)
        where = ["account_id = %s"]
        params = [account_id]
        if contact_id:
            where.append("contact_id = %s")
            params.append(contact_id)
        cur.execute(
            f"SELECT {cols} FROM whatsapp_invoices WHERE {' AND '.join(where)} ORDER BY requested_at DESC",
            params,
        )
        return [_row_to_invoice(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_invoice(invoice_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(INVOICE_COLUMNS)
        cur.execute(f"SELECT {cols} FROM whatsapp_invoices WHERE id = %s", (invoice_id,))
        row = cur.fetchone()
        return _row_to_invoice(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Criar / cancelar
# ---------------------------------------------------------------------------

# Campos fiscais do prestador que têm que estar cadastrados na conta antes
# de emitir a primeira nota (ver migração #32 pros 3 primeiros e #49 pros
# 3 últimos — mesma tela de "dados da empresa" que já existe).
_REQUIRED_ACCOUNT_FIELDS = (
    ("document_number", "CNPJ da clínica"),
    ("company_name", "razão social da clínica"),
    ("inscricao_municipal", "inscrição municipal"),
    ("regime_tributario", "regime tributário"),
    ("codigo_servico_municipal", "código de serviço municipal"),
)


def create_invoice(account_id, contact_id, requested_by, consultant_id,
                    service_description, amount, service_date):
    """Valida e grava com status 'pending' — quem realmente chama a Focus
    NFe é nfse_loop, que roda em background (mesmo desenho de
    document_summary.create + _process_one_document). Devolve
    (invoice_id, None) ou (None, mensagem)."""
    import server  # tardio — ver docstring do módulo

    service_description = (service_description or "").strip()
    if not service_description:
        return None, "Descrição do serviço é obrigatória."
    try:
        amount = Decimal(str(amount))
    except Exception:
        return None, "Valor inválido."
    if amount <= 0:
        return None, "Valor deve ser maior que zero."
    if not service_date:
        return None, "Data do serviço é obrigatória."

    patient = server.get_patient_record(contact_id)
    if not patient:
        return None, "Paciente não encontrado."
    if not patient.get("cpf"):
        return None, "Cadastre o CPF do paciente antes de emitir nota fiscal."

    account = server.get_account(account_id)
    if not account:
        return None, "Conta não encontrada."
    missing = [label for field, label in _REQUIRED_ACCOUNT_FIELDS if not account.get(field)]
    if missing:
        return None, ("Complete o cadastro fiscal da clínica antes de emitir nota fiscal "
                       f"(faltando: {', '.join(missing)}).")

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO whatsapp_invoices
               (account_id, contact_id, consultant_id,
                patient_name, patient_cpf, patient_address,
                service_description, amount, service_date, requested_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (account_id, contact_id, consultant_id,
             patient["name"], patient["cpf"], patient.get("address"),
             service_description, amount, service_date, requested_by),
        )
        invoice_id = cur.fetchone()[0]
        conn.commit()
        return invoice_id, None
    finally:
        conn.close()


def cancel_invoice(invoice_id, reason=None):
    """Nunca DELETE — mesma trilha de auditoria do recibo. Se a nota já
    tinha sido autorizada pela prefeitura, pede o cancelamento fiscal de
    verdade antes de marcar cancelado aqui; se ainda estava pending/
    processing (nunca chegou a ser autorizada), só marca local — não há
    nada fiscal a desfazer ainda."""
    invoice = get_invoice(invoice_id)
    if not invoice or invoice["status"] in ("cancelled",):
        return False, "Nota não encontrada ou já cancelada."
    if invoice["status"] == "authorized":
        try:
            nfse_focus.cancel_invoice(invoice["focus_ref"] or str(invoice_id), (reason or "Cancelado pela clínica")[:255])
        except nfse_focus.NfseError as e:
            return False, f"Falha ao cancelar na Focus NFe: {e}"

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_invoices SET status = 'cancelled', cancelled_at = NOW(), cancelled_reason = %s
               WHERE id = %s""",
            ((reason or "").strip()[:300] or None, invoice_id),
        )
        conn.commit()
        return cur.rowcount > 0, None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fila: reivindicar, emitir, consultar status
# ---------------------------------------------------------------------------

def _claim_next_pending(limit=5):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """WITH claimed AS (
                   SELECT id FROM whatsapp_invoices
                   WHERE status = 'pending'
                   ORDER BY requested_at ASC LIMIT %s FOR UPDATE SKIP LOCKED
               )
               UPDATE whatsapp_invoices d SET status = 'processing'
               FROM claimed WHERE d.id = claimed.id RETURNING d.id""",
            (limit,),
        )
        ids = [r[0] for r in cur.fetchall()]
        conn.commit()
        return ids
    finally:
        conn.close()


def _mark_error(invoice_id, message):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_invoices SET status = 'error', error_message = %s, completed_at = NOW()
               WHERE id = %s""",
            (str(message)[:2000], invoice_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_sent(invoice_id, focus_ref):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_invoices SET focus_ref = %s WHERE id = %s", (focus_ref, invoice_id))
        conn.commit()
    finally:
        conn.close()


def _mark_authorized(invoice_id, numero_nfse, codigo_verificacao, pdf_url, xml_url):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_invoices
               SET status = 'authorized', numero_nfse = %s, codigo_verificacao = %s,
                   pdf_url = %s, xml_url = %s, completed_at = NOW()
               WHERE id = %s""",
            (numero_nfse, codigo_verificacao, pdf_url, xml_url, invoice_id),
        )
        conn.commit()
    finally:
        conn.close()


def _build_dps_payload(invoice, account):
    """Monta o corpo da DPS (Declaração de Prestação de Serviço) — forma
    exata (nomes de campo) a confirmar contra doc.focusnfe.com.br/
    reference/nfse-nacional e o guia de Divinópolis antes da primeira
    emissão real; a estrutura prestador/tomador/servico abaixo segue o
    padrão que a Focus NFe documenta pros outros tipos de nota."""
    return {
        "prestador": {
            "cnpj": account.get("document_number"),
            "inscricao_municipal": account.get("inscricao_municipal"),
            "razao_social": account.get("company_name"),
        },
        "tomador": {
            "cpf": invoice["patient_cpf"],
            "razao_social": invoice["patient_name"],
            "endereco": invoice.get("patient_address"),
        },
        "servico": {
            "discriminacao": invoice["service_description"],
            "codigo_tributario_municipio": account.get("codigo_servico_municipal"),
            "valor_servicos": invoice["amount"],
            "data_competencia": invoice["service_date"],
        },
        "regime_tributario": account.get("regime_tributario"),
    }


def _process_one_invoice(invoice_id):
    import server  # tardio — ver docstring do módulo

    invoice = get_invoice(invoice_id)
    if not invoice:
        return
    account = server.get_account(invoice["account_id"])
    try:
        payload = _build_dps_payload(invoice, account or {})
        resp = nfse_focus.emit_invoice(invoice_id, payload)
        # Nome exato do campo de referência na resposta a confirmar contra
        # a doc real — usamos o próprio invoice_id como referência (é o que
        # mandamos em emit_invoice), então é seguro cair nele por padrão.
        focus_ref = resp.get("ref") or str(invoice_id)
        _mark_sent(invoice_id, focus_ref)
    except nfse_focus.NfseError as e:
        _mark_error(invoice_id, str(e))
        server.log_event(invoice["account_id"], "nfse_emit_failed", level="error",
                          detail={"invoice_id": invoice_id, "error": str(e)})


def _poll_processing_invoices():
    import server  # tardio — ver docstring do módulo

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, account_id, focus_ref FROM whatsapp_invoices WHERE status = 'processing' AND focus_ref IS NOT NULL"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    for invoice_id, account_id, focus_ref in rows:
        try:
            resp = nfse_focus.get_invoice_status(focus_ref)
        except nfse_focus.NfseError as e:
            server.log_event(account_id, "nfse_status_check_failed", level="warning",
                              detail={"invoice_id": invoice_id, "error": str(e)})
            continue
        # Valores de "status" e nomes de campo abaixo a confirmar contra a
        # resposta real da Focus NFe (sandbox de homologação) antes de ir
        # pra produção — mantidos como um único ponto de ajuste.
        remote_status = (resp.get("status") or "").lower()
        if remote_status in ("autorizado", "authorized"):
            _mark_authorized(
                invoice_id,
                numero_nfse=resp.get("numero"),
                codigo_verificacao=resp.get("codigo_verificacao"),
                pdf_url=resp.get("url") or resp.get("pdf_url"),
                xml_url=resp.get("caminho_xml_nota_fiscal") or resp.get("xml_url"),
            )
        elif remote_status in ("erro", "erro_autorizacao", "cancelado", "denegado"):
            _mark_error(invoice_id, resp.get("mensagem_sefaz") or resp.get("mensagem") or remote_status)
        # qualquer outro status (ex.: "processando_autorizacao") — deixa
        # 'processing', tenta de novo na próxima rodada.


def nfse_loop():
    import server  # tardio — ver docstring do módulo

    while True:
        try:
            for invoice_id in _claim_next_pending():
                threading.Thread(target=_process_one_invoice, args=(invoice_id,), daemon=True).start()
            _poll_processing_invoices()
        except Exception as e:
            server.log_event(None, "nfse_loop_error", level="error", detail={"error": str(e)})
        time.sleep(POLL_INTERVAL_SECONDS)
