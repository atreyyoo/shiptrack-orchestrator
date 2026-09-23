"""Renders a neat, one-page PDF summary of a submitted Purchase Requisition
— the main fields only, human-readable, not a raw code dump. Built entirely
from the row stored in purchase_requisitions (app/main.py's `payload` +
`fields_snapshot`, recorded at submission time by app/store.py), so the PDF
reflects exactly what was actually submitted, independent of any later
change to code/label mappings or master data.

Plain function, no I/O — app/main.py's GET /purchase-requisitions/{id}/pdf
is the only caller, and does the DB read and the download-response wrapping.
"""
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_LABEL_COLOR = colors.HexColor("#374151")
_RULE_COLOR = colors.HexColor("#e5e7eb")
_MUTED_COLOR = colors.HexColor("#6b7280")


def _field_table(rows: list[list[str]]) -> Table:
    table = Table(rows, colWidths=[50 * mm, 110 * mm])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("TEXTCOLOR", (0, 0), (0, -1), _LABEL_COLOR),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, -2), 0.5, _RULE_COLOR),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _label(fields: dict, field: str, key: str = "label") -> str:
    return (fields.get(field) or {}).get(key, "") or ""


def _vendor_line(fields: dict) -> str:
    """"Vendor Name (VND-1002) — 58900.00 MT" if a vendor was chosen (see
    app/tools/pr_intake_tool.py's "vendor" field), or a plain note that the
    catalog rate was used when the material had no vendor pricing to pick
    from — fields["vendor"] is None in that case, not absent."""
    vendor = fields.get("vendor")
    if not vendor:
        return "— (no vendor pricing on file; catalog rate used)"
    return f"{vendor.get('vendor_name', '')} ({vendor.get('vendor_code', '')}) — {_fmt_number(vendor.get('price', ''))} {vendor.get('uom_code', '')}".strip()


def _fmt_number(value) -> str:
    """Drops a pointless trailing ".0" on whole numbers (10.0 MT reads worse
    than 10 MT) while keeping real decimals (59640.5) intact."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def build_pr_pdf(row: dict) -> bytes:
    """`row` is the dict returned by app.store.get_purchase_requisition()."""
    payload = row["payload"]
    fields = row.get("fields_snapshot") or {}
    header = payload.get("jsonHeader", {})
    details = payload.get("jsonDetails") or [{}]
    detail = details[0]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
        title=f"Purchase Requisition {row.get('pr_number') or ''}".strip(),
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PRTitle", parent=styles["Title"], fontSize=20, spaceAfter=2)
    subtitle_style = ParagraphStyle("PRSubtitle", parent=styles["Normal"], textColor=_MUTED_COLOR, spaceAfter=16)
    section_style = ParagraphStyle("Section", parent=styles["Heading2"], fontSize=13, spaceBefore=16, spaceAfter=6)
    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=_MUTED_COLOR)

    pr_number = row.get("pr_number") or "(submission failed)"
    status = (row.get("status") or "").upper()
    created = row.get("created_at")
    created_str = created.strftime("%d %b %Y, %H:%M") if isinstance(created, datetime) else str(created or "")

    story = [
        Paragraph("Purchase Requisition", title_style),
        Paragraph(f"{pr_number} &nbsp;·&nbsp; {status} &nbsp;·&nbsp; {created_str}", subtitle_style),
        Paragraph("Header", section_style),
        _field_table(
            [
                ["Job", f"{header.get('hpR_Job_Code', '')} — {_label(fields, 'job', 'job_name')}"],
                ["Warehouse", f"{header.get('hpR_Warehouse_Code', '')} — {_label(fields, 'warehouse', 'warehouse_name')}"],
                ["Type", _label(fields, "pr_category") or str(header.get("hpR_PR_Category_Detail_Code", ""))],
                ["Planning Category", _label(fields, "planning_category") or str(header.get("hpR_Planning_Category_Type_Detail_Code", ""))],
                ["Supply At", _label(fields, "supply_at") or str(header.get("hpR_Material_Supply_Detail_Code", ""))],
                ["Own Premises", header.get("hpR_Own_Premises", "")],
                ["Remarks", header.get("hpR_Remarks") or "—"],
                ["Total Value", header.get("hpR_PR_Value", "")],
            ]
        ),
        Paragraph("Line Item", section_style),
        _field_table(
            [
                ["Material", f"{detail.get('dpR_Material_Code', '')} — {_label(fields, 'material', 'description')}"],
                ["Vendor", _vendor_line(fields)],
                ["WBS", f"{detail.get('dpR_WBS_Code', '')} — {_label(fields, 'wbs', 'description')}"],
                ["Cost Package", detail.get("dpR_Cost_Package_Code", "")],
                ["Quantity", f"{_fmt_number(detail.get('dpR_Qty', ''))} {detail.get('dpR_UOM_Code', '')}".strip()],
                ["Rate", _fmt_number(detail.get("dpR_Suggested_Rate", ""))],
                ["Line Value", _fmt_number(detail.get("dpR_Value", ""))],
            ]
        ),
        Spacer(1, 14 * mm),
        Paragraph(
            "Generated by the ShipTrack Draft PR Agent. For reference only — the ERP submission above is the system of record.",
            footer_style,
        ),
    ]

    doc.build(story)
    return buf.getvalue()
