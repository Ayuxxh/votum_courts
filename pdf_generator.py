import io
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)


def _build_cause_list_table(
    entries: List[Dict[str, Any]],
    styles,
    *,
    include_orders: bool | None = None,
) -> Table:
    """
    Build a cause list table with wrapped text cells.
    """
    has_orders = any(e.get("orders") and e.get("orders") != "-" for e in entries) if include_orders is None else include_orders

    if has_orders:
        headers = ["S.No", "Case No", "Coram", "Party Name", "Orders / Remarks"]
    else:
        headers = ["S.No", "Case No", "Coram", "Party Name", "Item No"]

    data = [headers]

    body_style = ParagraphStyle(
        "CauseListBody",
        parent=styles["BodyText"],
        fontSize=9,
        leading=11,
    )
    link_style = ParagraphStyle(
        "CauseListLink",
        parent=body_style,
        textColor=HexColor("#2563eb"),
        underline=True,
    )
    small_style = ParagraphStyle(
        "CauseListSmall",
        parent=styles["BodyText"],
        fontSize=8,
        leading=10,
    )

    for idx, entry in enumerate(entries, start=1):
        case_no = str(entry.get("case_no", "-"))
        case_url = str(entry.get("case_url") or "").strip()
        if case_url:
            case_cell = Paragraph(
                f'<link href="{case_url}"><u>{escape(case_no)}</u></link>',
                link_style,
            )
        else:
            case_cell = Paragraph(escape(case_no), body_style)

        row = [
            Paragraph(str(entry.get("sno", idx)), body_style),
            case_cell,
            Paragraph(escape(str(entry.get("court_name", "-"))), body_style),
            Paragraph(escape(str(entry.get("party_name", "-"))), body_style),
        ]
        if has_orders:
            row.append(Paragraph(str(entry.get("orders", "-")), small_style))
        else:
            row.append(Paragraph(escape(str(entry.get("item_no", "-"))), body_style))
        data.append(row)

    table_style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#1f2937")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 11),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#f8fafc"), HexColor("#eef2f7")]),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#cbd5e1")),
            ("ALIGN", (0, 1), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 1), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
        ]
    )

    # Available width ~780 (Landscape A4).
    if has_orders:
        col_widths = [40, 130, 160, 200, 250]
    else:
        col_widths = [40, 150, 180, 330, 80]

    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(table_style)
    return t

def generate_cause_list_pdf(
    entries: List[Dict[str, Any]],
    title: str,
    subtitle: Optional[str] = None,
    *,
    include_orders: bool | None = None,
) -> bytes:
    """
    Generate a PDF for the cause list / hearing list.
    
    entries: List of dicts with keys:
        - sno (Serial Number / Index)
        - case_no
        - court_name
        - item_no (optional)
        - orders (optional)
        - text (optional - raw text snippet)
        - case_url (optional - hyperlink for case_no)
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30
    )
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CauseListTitle",
        parent=styles["Title"],
        fontSize=18,
        leading=22,
        alignment=1,
        textColor=HexColor("#0f172a"),
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        "CauseListSubtitle",
        parent=styles["Heading2"],
        fontSize=11,
        leading=14,
        alignment=1,
        textColor=HexColor("#475569"),
        spaceAfter=12,
    )
    elements = []
    
    # Title
    elements.append(Paragraph(title, title_style))
    if subtitle:
        elements.append(Paragraph(subtitle, subtitle_style))
    elements.append(Spacer(1, 14))
    
    t = _build_cause_list_table(entries, styles, include_orders=include_orders)
    elements.append(t)
    
    doc.build(elements)
    buffer.seek(0)
    return buffer.read()


def generate_grouped_cause_list_pdf(
    entries: List[Dict[str, Any]],
    title: str,
    subtitle: Optional[str] = None,
    group_keys: tuple[str, str] = ("listing_date", "court_name"),
    *,
    include_orders: bool | None = None,
) -> bytes:
    """
    Generate a PDF with one table per (listing_date, court_name) group.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CauseListTitle",
        parent=styles["Title"],
        fontSize=18,
        leading=22,
        alignment=1,
        textColor=HexColor("#0f172a"),
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        "CauseListSubtitle",
        parent=styles["Heading2"],
        fontSize=11,
        leading=14,
        alignment=1,
        textColor=HexColor("#475569"),
        spaceAfter=12,
    )
    section_style = ParagraphStyle(
        "CauseListSection",
        parent=styles["Heading3"],
        fontSize=12,
        leading=16,
        textColor=HexColor("#1e293b"),
        spaceBefore=6,
        spaceAfter=6,
    )
    elements = []

    elements.append(Paragraph(title, title_style))
    if subtitle:
        elements.append(Paragraph(subtitle, subtitle_style))
    elements.append(Spacer(1, 14))

    # Group entries by (listing_date, court_name)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        k1 = str(entry.get(group_keys[0], "")).strip()
        k2 = str(entry.get(group_keys[1], "")).strip()
        key = f"{k1}__{k2}"
        grouped.setdefault(key, []).append(entry)

    # Stable order by date then court
    def sort_key(item):
        k, _ = item
        parts = k.split("__", 1)
        return (parts[0], parts[1] if len(parts) > 1 else "")

    for key, group in sorted(grouped.items(), key=sort_key):
        date_label, court_label = key.split("__", 1)
        heading = f"{date_label} — {court_label}" if court_label else date_label
        elements.append(Paragraph(heading, section_style))
        elements.append(Spacer(1, 8))
        elements.append(_build_cause_list_table(group, styles, include_orders=include_orders))
        elements.append(Spacer(1, 16))

    doc.build(elements)
    buffer.seek(0)
    return buffer.read()


def generate_daily_matters_pdf_2(
    matters: List[Dict[str, Any]],
    title: str,
    subtitle: Optional[str] = None,
) -> bytes:
    """
    PDF-2 (daily): same serial/registration order as PDF-1, plus next listing date + order links.
    Columns:
      1) Serial No
      2) Registration No
      3) Next Listing Date
      4) Links to Orders Passed
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30,
    )

    styles = getSampleStyleSheet()
    elements: list = []

    elements.append(Paragraph(title, styles["Title"]))
    if subtitle:
        elements.append(Paragraph(subtitle, styles["Heading2"]))
    elements.append(Spacer(1, 20))

    headers = ["S.No", "Registration No", "Party Name", "Next Listing Date", "Orders"]
    data: list = [headers]

    link_style = styles["BodyText"]

    for idx, m in enumerate(matters, start=1):
        orders = m.get("orders") or []
        if isinstance(orders, str):
            orders_html = escape(orders)
        else:
            parts: list[str] = []
            for jdx, order in enumerate(orders, start=1):
                if not isinstance(order, dict):
                    continue
                url = order.get("url") or order.get("document_url") or order.get("link")
                if not url:
                    continue
                label = (
                    order.get("label")
                    or order.get("date")
                    or f"Order {jdx}"
                )
                parts.append(
                    f'<link href="{escape(str(url))}">{escape(str(label))}</link>'
                )
            orders_html = "<br/>".join(parts) if parts else "-"

        data.append(
            [
                str(m.get("sno", idx)),
                m.get("registration_no", "-"),
                m.get("party_name", "-"),
                m.get("next_listing_date", "-"),
                Paragraph(orders_html, link_style),
            ]
        )

    table_style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 12),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 12),
            ("BACKGROUND", (0, 1), (-1, -1), colors.beige),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("ALIGN", (1, 1), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("WORDWRAP", (0, 0), (-1, -1), True),
        ]
    )

    col_widths = [40, 150, 200, 100, 290]
    t = Table(data, colWidths=col_widths)
    t.setStyle(table_style)
    elements.append(t)

    doc.build(elements)
    buffer.seek(0)
    return buffer.read()
