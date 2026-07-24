"""Geração de PDF de recibos médicos e da declaração anual para Imposto de Renda.

Os dois PDFs são gerados inteiramente em memória (BytesIO) — nunca gravados
em disco — e recebem os dados já resolvidos (snapshot do recibo, ou lista de
recibos + snapshots do paciente/consultor para a declaração anual). Este
módulo não acessa o banco nem conhece account_id/contact_id — isso é
responsabilidade de quem chama.
"""

from datetime import datetime
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

try:
    from num2words import num2words
except ImportError:  # pragma: no cover - só falta se a dependência não foi instalada
    num2words = None

MONTHS_PT = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


def _fmt_date(d):
    if d is None:
        return ""
    return d.strftime("%d/%m/%Y")


def _fmt_date_extenso(d):
    if d is None:
        return ""
    return f"{d.day} de {MONTHS_PT[d.month - 1]} de {d.year}"


def _fmt_brl(amount):
    s = f"{Decimal(str(amount)):,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _valor_por_extenso(amount):
    if num2words is None:
        return ""
    try:
        texto = num2words(float(amount), lang="pt_BR", to="currency", currency="BRL")
        return texto
    except Exception:
        return ""


def _esc(value):
    return escape(str(value)) if value else ""


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReciboTitulo", fontSize=14, leading=18, spaceAfter=4,
        fontName="Helvetica-Bold",
    ))
    styles.add(ParagraphStyle(
        name="ReciboCorpo", fontSize=11, leading=16, spaceAfter=10,
        fontName="Helvetica",
    ))
    styles.add(ParagraphStyle(
        name="ReciboAssinatura", fontSize=10, leading=14, alignment=1,
        fontName="Helvetica",
    ))
    styles.add(ParagraphStyle(
        name="ReciboRodape", fontSize=7.5, leading=10, textColor=colors.grey,
        fontName="Helvetica", spaceBefore=14,
    ))
    styles.add(ParagraphStyle(
        name="ReciboTabelaCelula", fontSize=9, leading=11, fontName="Helvetica",
    ))
    return styles


def _provider_block_html(receipt, professional_term):
    lines = [f"<b>{_esc(receipt['consultant_name'])}</b>"]
    line2 = f"CPF: {_esc(receipt['consultant_cpf'])} &nbsp;&nbsp; CRM: {_esc(receipt['consultant_crm'])}"
    lines.append(line2)
    if receipt.get("consultant_address"):
        lines.append(_esc(receipt["consultant_address"]))
    if receipt.get("company_name"):
        lines.append(f"Estabelecimento: {_esc(receipt['company_name'])}")
    return "<br/>".join(lines)


def generate_receipt_pdf(receipt: dict, professional_term: str = "Médico") -> bytes:
    """receipt: uma linha de whatsapp_receipts (dict). professional_term:
    singular da nomenclatura configurada (ex: "Médico", "Consultor")."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2.5 * cm, rightMargin=2.5 * cm,
        topMargin=2.5 * cm, bottomMargin=2 * cm,
    )
    styles = _styles()
    story = []

    story.append(Paragraph(f"RECIBO Nº {receipt['id']}", styles["ReciboTitulo"]))
    story.append(Paragraph(
        f"Emitido em {_fmt_date(receipt['issued_at'].date() if hasattr(receipt['issued_at'], 'date') else receipt['issued_at'])}",
        styles["Normal"],
    ))
    story.append(Spacer(1, 0.8 * cm))

    valor_brl = _fmt_brl(receipt["amount"])
    valor_extenso = _valor_por_extenso(receipt["amount"])
    valor_extenso_txt = f" ({valor_extenso})" if valor_extenso else ""

    corpo = (
        f"Recebi de <b>{_esc(receipt['patient_name'])}</b>, portador(a) do CPF nº "
        f"{_esc(receipt['patient_cpf'])}, a importância de <b>R$ {valor_brl}</b>"
        f"{valor_extenso_txt}, referente a {_esc(receipt['service_description'])}, "
        f"prestado(a) em {_fmt_date(receipt['service_date'])}."
    )
    story.append(Paragraph(corpo, styles["ReciboCorpo"]))
    story.append(Spacer(1, 1.2 * cm))

    story.append(Paragraph(_provider_block_html(receipt, professional_term), styles["Normal"]))
    story.append(Spacer(1, 1.6 * cm))

    story.append(Paragraph(_fmt_date_extenso(receipt["issued_at"].date() if hasattr(receipt["issued_at"], "date") else receipt["issued_at"]) + ".", styles["Normal"]))
    story.append(Spacer(1, 1.4 * cm))

    story.append(Paragraph("_____________________________________", styles["ReciboAssinatura"]))
    story.append(Paragraph(
        f"{_esc(receipt['consultant_name'])}<br/>{_esc(professional_term)} — CRM {_esc(receipt['consultant_crm'])}",
        styles["ReciboAssinatura"],
    ))

    issued_ts = receipt["issued_at"]
    issued_ts_fmt = issued_ts.strftime("%d/%m/%Y %H:%M") if hasattr(issued_ts, "strftime") else str(issued_ts)
    story.append(Paragraph(
        f"Documento emitido eletronicamente pelo sistema Oráculo em {issued_ts_fmt}. "
        f"Recibo nº {receipt['id']} — válido como comprovante para fins de dedução de "
        f"despesas médicas na Declaração de Imposto de Renda, nos termos da legislação vigente.",
        styles["ReciboRodape"],
    ))

    doc.build(story)
    return buf.getvalue()


def generate_annual_statement_pdf(consultant_snapshot: dict, patient_snapshot: dict,
                                   receipts: list, year: int,
                                   professional_term: str = "Médico") -> bytes:
    """consultant_snapshot/patient_snapshot: dicts com os mesmos campos de
    snapshot presentes em whatsapp_receipts (tirados do recibo mais recente
    do conjunto). receipts: lista de linhas ativas do ano, ordenada por
    service_date."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2.5 * cm, rightMargin=2.5 * cm,
        topMargin=2.5 * cm, bottomMargin=2 * cm,
    )
    styles = _styles()
    story = []

    story.append(Paragraph("DECLARAÇÃO PARA FINS DE IMPOSTO DE RENDA", styles["ReciboTitulo"]))
    story.append(Paragraph(f"Ano-calendário: {year}", styles["Normal"]))
    story.append(Spacer(1, 0.8 * cm))

    corpo = (
        f"Declaro, para fins de comprovação perante a Receita Federal do Brasil, que "
        f"<b>{_esc(patient_snapshot['patient_name'])}</b>, CPF nº {_esc(patient_snapshot['patient_cpf'])}, "
        f"foi paciente sob meus cuidados profissionais durante o ano de {year}, tendo pago os "
        f"valores abaixo discriminados, referentes a serviços de natureza médica prestados:"
    )
    story.append(Paragraph(corpo, styles["ReciboCorpo"]))

    table_data = [["Data", "Recibo nº", "Serviço prestado", "Valor (R$)"]]
    total = Decimal("0")
    for r in receipts:
        table_data.append([
            _fmt_date(r["service_date"]),
            str(r["id"]),
            Paragraph(_esc(r["service_description"]), styles["ReciboTabelaCelula"]),
            _fmt_brl(r["amount"]),
        ])
        total += Decimal(str(r["amount"]))
    table_data.append(["", "", "TOTAL", _fmt_brl(total)])

    table = Table(table_data, colWidths=[2.4 * cm, 2 * cm, 8.6 * cm, 3 * cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2744")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.5 * cm))

    total_extenso = _valor_por_extenso(total)
    total_extenso_txt = f" ({total_extenso})" if total_extenso else ""
    story.append(Paragraph(
        f"Total geral: <b>R$ {_fmt_brl(total)}</b>{total_extenso_txt}.",
        styles["ReciboCorpo"],
    ))
    story.append(Spacer(1, 1 * cm))

    story.append(Paragraph(_provider_block_html(consultant_snapshot, professional_term), styles["Normal"]))
    story.append(Spacer(1, 1.6 * cm))

    story.append(Paragraph(_fmt_date_extenso(datetime.now().date()) + ".", styles["Normal"]))
    story.append(Spacer(1, 1.4 * cm))

    story.append(Paragraph("_____________________________________", styles["ReciboAssinatura"]))
    story.append(Paragraph(
        f"{_esc(consultant_snapshot['consultant_name'])}<br/>{_esc(professional_term)} — CRM {_esc(consultant_snapshot['consultant_crm'])}",
        styles["ReciboAssinatura"],
    ))

    story.append(Paragraph(
        f"Documento emitido eletronicamente pelo sistema Oráculo em {datetime.now().strftime('%d/%m/%Y %H:%M')}.",
        styles["ReciboRodape"],
    ))

    doc.build(story)
    return buf.getvalue()
