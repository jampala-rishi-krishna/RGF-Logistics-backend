from __future__ import annotations

from html import escape
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

HEADERS = ["Expected Shipment Date", "Sales Order#", "Customer Name", "Item Description", "SKU", "Quantity", "Unit", "City", "Shipping Address", "Fulfillment Type", "Order Status", "Invoiced", "Payment", "Packed", "Shipped", "Amount"]
RARE_RED = colors.HexColor("#86000B")
BEIGE = colors.HexColor("#FFEBCE")
HUNTER_GREEN = colors.HexColor("#33673B")
BRAND_WHITE = colors.HexColor("#FFFFFF")
BRAND_BLACK = colors.HexColor("#1B2419")

def _text(value) -> str:
    return "" if value is None else str(value)

def _date(value) -> str:
    return value.strftime("%d %b %Y") if hasattr(value, "strftime") else _text(value)

def _status_color(value) -> colors.Color:
    normalized = _text(value).lower()
    if normalized in {"confirmed", "acknowledged"}: return HUNTER_GREEN
    if normalized == "void": return RARE_RED
    return BRAND_BLACK

def _address(value) -> str:
    item = value[0] if isinstance(value, list) and value else value
    if not isinstance(item, dict): return _text(item)
    return ", ".join(_text(item.get(key)) for key in ("address", "street_address", "city", "state", "zip", "country") if item.get(key))

def flatten_order(order) -> list[list]:
    raw = order.raw_json or {}
    # Export only real Zoho line items. UI separator rows or empty objects must
    # never become blank spreadsheet/PDF rows.
    source_items = raw.get("line_items") or []
    items = [item for item in source_items if isinstance(item, dict) and any(item.get(key) not in (None, "") for key in ("name", "item_description", "sku", "quantity", "unit", "item_total", "amount"))]
    shipping = _address(raw.get("shipping_address") or order.shipping_address)
    city = ""
    address_value = raw.get("shipping_address") or order.shipping_address
    address_item = address_value[0] if isinstance(address_value, list) and address_value else address_value
    if isinstance(address_item, dict): city = _text(address_item.get("city"))
    invoice_status = _text(getattr(order, "invoice_status", "") or raw.get("invoice_status") or raw.get("invoiced_status")).lower()
    payment_status = _text(getattr(order, "payment_status", "") or raw.get("payment_status") or raw.get("paid_status")).lower()
    shipment_status = _text(getattr(order, "shipment_status", "") or raw.get("shipment_status") or raw.get("shipping_status") or raw.get("status")).lower()
    invoices = raw.get("invoices") if isinstance(raw.get("invoices"), list) else []
    packages = raw.get("packages") if isinstance(raw.get("packages"), list) else []
    invoiced = "Yes" if any(item.get("is_invoiced") is True or (item.get("quantity") and item.get("quantity_invoiced", 0) >= item.get("quantity")) for item in items) or any(_text(invoice.get("status")).lower() not in {"draft", "void", "cancelled", "canceled"} for invoice in invoices) or "invoiced" in invoice_status or "billed" in invoice_status else "No"
    payment = "Yes" if "paid" in payment_status or "payment received" in payment_status or any(invoice.get("balance") is not None and float(invoice.get("balance") or 0) <= 0 for invoice in invoices) else "No"
    packed = "Yes" if any(item.get("quantity") and item.get("quantity_packed", 0) >= item.get("quantity") for item in items) or any(value in _text(package.get("status") or package.get("detailed_status")).lower() for package in packages for value in ("packed", "fulfilled", "shipped", "delivered")) or any(value in shipment_status for value in ("packed", "shipped", "delivered")) else "No"
    shipped = "Yes" if any(item.get("quantity") and item.get("quantity_shipped", 0) >= item.get("quantity") for item in items) or any(value in _text(package.get("status") or package.get("detailed_status")).lower() for package in packages for value in ("shipped", "fulfilled", "delivered")) or any(value in shipment_status for value in ("shipped", "delivered")) else "No"
    return [[_date(order.expected_shipment_date), order.salesorder_number, order.customer_name, item.get("name") or item.get("item_description"), item.get("sku"), item.get("quantity"), item.get("unit"), city, shipping, raw.get("fulfillment_type"), order.order_status, invoiced, payment, packed, shipped, item.get("item_total") or order.total] for item in items]

def make_excel(rows: list[list], caption: str) -> BytesIO:
    book = Workbook(); sheet = book.active; sheet.title = "Sales Orders"
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS)); sheet.cell(1, 1, "Rare Global Food Trading Corp.").font = Font(bold=True, size=16)
    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(HEADERS)); sheet.cell(2, 1, caption)
    for column, header in enumerate(HEADERS, 1):
        cell = sheet.cell(4, column, header); cell.font = Font(bold=True, color="FFFFFF"); cell.fill = PatternFill("solid", fgColor="111111")
    for row in rows: sheet.append(row)
    sheet.freeze_panes = "A5"
    for column in range(1, len(HEADERS) + 1): sheet.column_dimensions[get_column_letter(column)].width = min(34, max(12, max(len(_text(sheet.cell(row, column).value)) for row in range(4, sheet.max_row + 1)) + 2))
    output = BytesIO(); book.save(output); output.seek(0); return output

def make_pdf(rows: list[list], caption: str) -> BytesIO:
    output = BytesIO(); doc = SimpleDocTemplate(output, pagesize=landscape(A3), rightMargin=0.35 * inch, leftMargin=0.35 * inch, topMargin=0.35 * inch, bottomMargin=0.35 * inch)
    styles = getSampleStyleSheet(); title = ParagraphStyle("ExportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=RARE_RED); heading = ParagraphStyle("ExportHeading", parent=styles["Heading2"], fontName="Helvetica", fontSize=13, leading=16, textColor=BRAND_BLACK); caption_style = ParagraphStyle("ExportCaption", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=11, textColor=BRAND_BLACK)
    # Paragraph backgrounds must stay unset: ReportLab would paint them only
    # behind the text bounding box. TableStyle owns all cell/row backgrounds.
    cell_style = ParagraphStyle("ExportCell", parent=styles["Normal"], fontName="Helvetica", fontSize=8, leading=9, textColor=BRAND_BLACK)
    header_style = ParagraphStyle("ExportHeader", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=9, textColor=BRAND_WHITE)
    story = [Paragraph("<b>Rare Global Food Trading Corp.</b>", title), Paragraph("Sales Orders - Load Planning", heading), Paragraph(escape(caption), caption_style), Spacer(1, 0.15 * inch)]
    # A3 landscape gives the complete operational export room for all columns.
    widths = [52, 70, 95, 105, 55, 40, 35, 55, 145, 65, 60, 45, 45, 42, 42, 65]
    data = [[Paragraph(escape(header), header_style) for header in HEADERS]]
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            style = cell_style
            if index == 10:
                style = cell_style.clone(f"status-{_text(value)}")
                style.textColor = _status_color(value)
            cells.append(Paragraph(escape(_text(value)), style))
        data.append(cells)
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), RARE_RED), ("TEXTCOLOR", (0, 0), (-1, 0), BRAND_WHITE), ("TEXTCOLOR", (0, 1), (-1, -1), BRAND_BLACK), ("BACKGROUND", (0, 1), (-1, -1), BRAND_WHITE), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("GRID", (0, 0), (-1, -1), 0.25, BEIGE), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [BRAND_WHITE, BEIGE])]))
    story.append(table)

    def page_footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(BEIGE)
        canvas.setLineWidth(0.6)
        canvas.line(document.leftMargin, 0.31 * inch, landscape(A3)[0] - document.rightMargin, 0.31 * inch)
        canvas.setFillColor(BRAND_BLACK)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(document.leftMargin, 0.18 * inch, "Rare Global Food Trading Corp. - Sales Orders - Load Planning")
        canvas.drawRightString(landscape(A3)[0] - document.rightMargin, 0.18 * inch, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer); output.seek(0); return output
