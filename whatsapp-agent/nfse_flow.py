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
    "service_catalog_id", "codigo_tributacao_nacional", "codigo_servico_municipal", "codigo_nbs",
]

SERVICE_CATALOG_COLUMNS = [
    "id", "account_id", "name", "description_template",
    "codigo_tributacao_nacional", "codigo_servico_municipal", "codigo_nbs",
    "active", "created_at",
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


def get_invoices(account_id, contact_id=None, status=None, q=None, date_from=None, date_to=None, limit=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(INVOICE_COLUMNS)
        where = ["account_id = %s"]
        params = [account_id]
        if contact_id:
            where.append("contact_id = %s")
            params.append(contact_id)
        if status:
            where.append("status = %s")
            params.append(status)
        if q:
            where.append("patient_name ILIKE %s")
            params.append(f"%{q}%")
        if date_from:
            where.append("service_date >= %s")
            params.append(date_from)
        if date_to:
            where.append("service_date <= %s")
            params.append(date_to)
        sql = f"SELECT {cols} FROM whatsapp_invoices WHERE {' AND '.join(where)} ORDER BY requested_at DESC"
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        cur.execute(sql, params)
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
# 2 seguintes — mesma tela de "dados da empresa" que já existe). O código
# de serviço municipal saiu daqui (ver _resolve_service_codes) — agora pode
# vir do catálogo por nota em vez de só do valor único da conta.
_REQUIRED_ACCOUNT_FIELDS = (
    ("document_number", "CNPJ da clínica"),
    ("company_name", "razão social da clínica"),
    ("inscricao_municipal", "inscrição municipal"),
    ("regime_tributario", "regime tributário"),
)


def _resolve_service_codes(account_id, service_catalog_id, account):
    """Resolve os 3 códigos fiscais que vão pra nota: se `service_catalog_id`
    for informado, usa os códigos daquela entrada (com fallback pro código
    municipal da conta se a entrada não tiver o dela — mesma migração
    gradual de quem ainda não cadastrou tudo no catálogo); sem
    `service_catalog_id`, usa só o código municipal da conta (comportamento
    de antes do catálogo existir). Devolve (dict com os 3 campos, None) ou
    (None, mensagem de erro)."""
    if service_catalog_id:
        entry = get_service_catalog_entry(service_catalog_id)
        if not entry or entry["account_id"] != account_id or not entry["active"]:
            return None, "Serviço do cadastro não encontrado."
        return {
            "codigo_tributacao_nacional": entry.get("codigo_tributacao_nacional"),
            "codigo_servico_municipal": entry.get("codigo_servico_municipal") or (account or {}).get("codigo_servico_municipal"),
            "codigo_nbs": entry.get("codigo_nbs"),
        }, None
    return {
        "codigo_tributacao_nacional": None,
        "codigo_servico_municipal": (account or {}).get("codigo_servico_municipal"),
        "codigo_nbs": None,
    }, None


def create_invoice(account_id, contact_id, requested_by, consultant_id,
                    service_description, amount, service_date, service_catalog_id=None):
    """Valida e grava com status 'pending' — quem realmente chama a Focus
    NFe é nfse_loop, que roda em background (mesmo desenho de
    document_summary.create + _process_one_document). `service_catalog_id`
    opcional grava um snapshot dos códigos fiscais daquela entrada na
    própria nota (ver _resolve_service_codes) — editar o catálogo depois
    não muda notas já criadas. Devolve (invoice_id, None) ou (None,
    mensagem)."""
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

    codes, error = _resolve_service_codes(account_id, service_catalog_id, account)
    if error:
        return None, error
    if not codes["codigo_servico_municipal"]:
        return None, ("Complete o cadastro fiscal da clínica antes de emitir nota fiscal "
                       "(faltando: código de serviço municipal).")

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO whatsapp_invoices
               (account_id, contact_id, consultant_id,
                patient_name, patient_cpf, patient_address,
                service_description, amount, service_date, requested_by,
                service_catalog_id, codigo_tributacao_nacional, codigo_servico_municipal, codigo_nbs)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (account_id, contact_id, consultant_id,
             patient["name"], patient["cpf"], patient.get("address"),
             service_description, amount, service_date, requested_by,
             service_catalog_id, codes["codigo_tributacao_nacional"],
             codes["codigo_servico_municipal"], codes["codigo_nbs"]),
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


_UNSET = object()


def update_invoice(invoice_id, service_description=None, amount=None, service_date=None, service_catalog_id=_UNSET):
    """Edição parcial — só aplica os campos passados. Só é permitida antes
    de qualquer autorização real (status 'pending') ou depois de uma
    tentativa que falhou ('error') — uma nota 'processing'/'authorized' já
    foi (ou está sendo) comunicada à prefeitura sob os dados originais, não
    dá pra editar retroativamente. Devolve (True, None) ou (False, mensagem)."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return False, "Nota não encontrada."
    if invoice["status"] not in ("pending", "error"):
        return False, "Só é possível editar notas ainda não transmitidas ou com erro."

    fields, params = [], []
    if service_description is not None:
        service_description = service_description.strip()
        if not service_description:
            return False, "Descrição do serviço é obrigatória."
        fields.append("service_description = %s")
        params.append(service_description)
    if amount is not None:
        try:
            amount = Decimal(str(amount))
        except Exception:
            return False, "Valor inválido."
        if amount <= 0:
            return False, "Valor deve ser maior que zero."
        fields.append("amount = %s")
        params.append(amount)
    if service_date is not None:
        if not service_date:
            return False, "Data do serviço é obrigatória."
        fields.append("service_date = %s")
        params.append(service_date)
    if service_catalog_id is not _UNSET:
        import server  # tardio — ver docstring do módulo
        account = server.get_account(invoice["account_id"])
        codes, error = _resolve_service_codes(invoice["account_id"], service_catalog_id, account)
        if error:
            return False, error
        fields += ["service_catalog_id = %s", "codigo_tributacao_nacional = %s",
                   "codigo_servico_municipal = %s", "codigo_nbs = %s"]
        params += [service_catalog_id, codes["codigo_tributacao_nacional"],
                   codes["codigo_servico_municipal"], codes["codigo_nbs"]]

    if not fields:
        return True, None

    conn = _conn()
    try:
        cur = conn.cursor()
        params.append(invoice_id)
        cur.execute(f"UPDATE whatsapp_invoices SET {', '.join(fields)} WHERE id = %s", params)
        conn.commit()
        return cur.rowcount > 0, None
    finally:
        conn.close()


def retry_invoice(invoice_id):
    """Reenvia uma nota que falhou — só a partir de 'error'. Só reseta pra
    'pending'; quem realmente reenvia é o mesmo nfse_loop de sempre, no
    próximo ciclo (_claim_next_pending só olha 'pending'). Como
    nfse_focus.emit_invoice usa o id da nota como referência (chave de
    idempotência), reenviar não duplica nada na Focus NFe mesmo que a
    tentativa anterior tenha ido parcialmente adiante."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return False, "Nota não encontrada."
    if invoice["status"] != "error":
        return False, "Só é possível retransmitir notas com erro."
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_invoices SET status = 'pending', error_message = NULL, completed_at = NULL
               WHERE id = %s""",
            (invoice_id,),
        )
        conn.commit()
        return cur.rowcount > 0, None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Catálogo de serviços fiscais
# ---------------------------------------------------------------------------

def _row_to_service_catalog(row):
    d = dict(zip(SERVICE_CATALOG_COLUMNS, row))
    d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
    return d


def list_service_catalog(account_id, only_active=True):
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(SERVICE_CATALOG_COLUMNS)
        where = "account_id = %s" + (" AND active" if only_active else "")
        cur.execute(f"SELECT {cols} FROM whatsapp_service_catalog WHERE {where} ORDER BY name", (account_id,))
        return [_row_to_service_catalog(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_service_catalog_entry(entry_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cols = ", ".join(SERVICE_CATALOG_COLUMNS)
        cur.execute(f"SELECT {cols} FROM whatsapp_service_catalog WHERE id = %s", (entry_id,))
        row = cur.fetchone()
        return _row_to_service_catalog(row) if row else None
    finally:
        conn.close()


def create_service_catalog_entry(account_id, name, description_template,
                                  codigo_tributacao_nacional=None, codigo_servico_municipal=None, codigo_nbs=None):
    name = (name or "").strip()
    description_template = (description_template or "").strip()
    if not name:
        return None, "Nome do serviço é obrigatório."
    if not description_template:
        return None, "Descrição padrão do serviço é obrigatória."
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO whatsapp_service_catalog
               (account_id, name, description_template, codigo_tributacao_nacional, codigo_servico_municipal, codigo_nbs)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (account_id, name, description_template,
             (codigo_tributacao_nacional or "").strip() or None,
             (codigo_servico_municipal or "").strip() or None,
             (codigo_nbs or "").strip() or None),
        )
        entry_id = cur.fetchone()[0]
        conn.commit()
        return entry_id, None
    finally:
        conn.close()


def update_service_catalog_entry(entry_id, name=None, description_template=None,
                                  codigo_tributacao_nacional=None, codigo_servico_municipal=None, codigo_nbs=None):
    fields, params = [], []
    if name is not None:
        name = name.strip()
        if not name:
            return False, "Nome do serviço é obrigatório."
        fields.append("name = %s")
        params.append(name)
    if description_template is not None:
        description_template = description_template.strip()
        if not description_template:
            return False, "Descrição padrão do serviço é obrigatória."
        fields.append("description_template = %s")
        params.append(description_template)
    for field, value in (
        ("codigo_tributacao_nacional", codigo_tributacao_nacional),
        ("codigo_servico_municipal", codigo_servico_municipal),
        ("codigo_nbs", codigo_nbs),
    ):
        if value is not None:
            fields.append(f"{field} = %s")
            params.append(value.strip() or None)
    if not fields:
        return True, None
    conn = _conn()
    try:
        cur = conn.cursor()
        params.append(entry_id)
        cur.execute(f"UPDATE whatsapp_service_catalog SET {', '.join(fields)} WHERE id = %s", params)
        conn.commit()
        return cur.rowcount > 0, None
    finally:
        conn.close()


def deactivate_service_catalog_entry(entry_id):
    """Soft delete — nunca DELETE, notas já emitidas podem referenciar essa
    linha (ver comentário da migração #52)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_service_catalog SET active = FALSE WHERE id = %s", (entry_id,))
        conn.commit()
        return cur.rowcount > 0
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
    padrão que a Focus NFe documenta pros outros tipos de nota.

    Códigos fiscais do serviço priorizam o snapshot gravado na própria nota
    (do catálogo escolhido na emissão, ver _resolve_service_codes) e caem
    pro valor da conta só pra notas antigas, de antes do catálogo existir.
    `codigo_tributacao_nacional`/`codigo_nbs` só entram no payload quando
    presentes — nome exato do(s) campo(s) também a confirmar."""
    servico = {
        "discriminacao": invoice["service_description"],
        "codigo_tributario_municipio": invoice.get("codigo_servico_municipal") or account.get("codigo_servico_municipal"),
        "valor_servicos": invoice["amount"],
        "data_competencia": invoice["service_date"],
    }
    if invoice.get("codigo_tributacao_nacional"):
        servico["codigo_tributacao_nacional"] = invoice["codigo_tributacao_nacional"]
    if invoice.get("codigo_nbs"):
        servico["codigo_nbs"] = invoice["codigo_nbs"]
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
        "servico": servico,
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
