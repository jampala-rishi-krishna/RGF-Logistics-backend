from __future__ import annotations

from html import escape
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from services.sales_order_location import address_lines, shipping_city

HEADERS = ["Expected Shipment Date", "Sales Order#", "Customer Name", "Item Description", "SKU", "Quantity", "Unit", "City", "Shipping Address", "Fulfillment Type", "Order Status", "Invoiced", "Payment", "Packed", "Shipped", "Amount"]
# Confirmed SO export: every column the Confirmed SO screen shows, in the layout dispatch uses.
CONFIRMED_HEADERS = ["Date", "Item", "SKU", "Quantity", "Unit", "SO Number", "Customer", "City", "Shipping Address", "Warehouse", "Mets Qty Available for Sale", "Glacier Qty Available for Sale", "Notes", "Fulfillment Type", "Truck", "Driver/Helper"]
CONFIRMED_STOCK_COLUMNS = (10, 11)
CONFIRMED_WIDTHS = [50, 100, 58, 42, 30, 62, 85, 52, 118, 68, 52, 56, 150, 62, 56, 80]
CYAN = colors.HexColor("#00FFFF")
NEGATIVE_RED = colors.HexColor("#C00000")
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
    return ", ".join(address_lines(value))

def _fulfillment_type(raw: dict) -> str:
    """Zoho stores this as a custom field (cf_fulfillment_type), not a top-level key."""
    hash_value = (raw.get("custom_field_hash") or {}).get("cf_fulfillment_type") if isinstance(raw.get("custom_field_hash"), dict) else None
    labelled = next((field.get("value") for field in raw.get("custom_fields") or [] if isinstance(field, dict) and str(field.get("label") or "").strip().lower() == "fulfillment type"), None)
    return _text(raw.get("fulfillment_type") or raw.get("cf_fulfillment_type") or hash_value or labelled or raw.get("cf_fulfillment_type_formatted") or raw.get("order_fulfillment_type"))


def _line_item_id(item: dict) -> str | None:
    nested = item.get("item") if isinstance(item.get("item"), dict) else {}
    value = item.get("item_id") or item.get("itemid") or nested.get("item_id") or nested.get("id")
    return str(value) if value else None


def confirmed_item_ids(orders) -> list[str]:
    ids: list[str] = []
    for order in orders:
        for item in ((getattr(order, "raw_json", None) or {}).get("line_items") or []):
            if isinstance(item, dict) and _line_item_id(item):
                ids.append(_line_item_id(item))
    return list(dict.fromkeys(ids))


def _line_warehouse(item: dict) -> str | None:
    nested = item.get("warehouse") if isinstance(item.get("warehouse"), dict) else {}
    return item.get("warehouse_name") or item.get("location_name") or nested.get("warehouse_name") or None


def flatten_confirmed_order(order, stock_of, truck_driver) -> list[list]:
    """One row per real Zoho line item, in CONFIRMED_HEADERS order. `stock_of(item_id)` returns
    {"mets", "glacier"} for that item (parsed for this order's branch); `truck_driver(order)` returns (truck, driver/helper)."""
    raw = getattr(order, "raw_json", None) or {}
    items = [item for item in (raw.get("line_items") or []) if isinstance(item, dict) and any(item.get(key) not in (None, "") for key in ("name", "item_description", "sku", "quantity", "unit"))]
    address_value = raw.get("shipping_address") or getattr(order, "shipping_address", None)
    address_item = address_value[0] if isinstance(address_value, list) and address_value else address_value
    city = _text(shipping_city(order))
    notes = _text(raw.get("notes") or raw.get("note") or raw.get("customer_notes")).strip()
    truck, driver = truck_driver(order)
    rows = []
    for item in items:
        item_id = _line_item_id(item)
        stock = (stock_of(item_id) if item_id else None) or {}
        rows.append([_date(order.expected_shipment_date), item.get("name") or item.get("item_description"), item.get("sku"), item.get("quantity"), item.get("unit"), order.salesorder_number, order.customer_name, city, _address(address_value), _line_warehouse(item), stock.get("mets"), stock.get("glacier"), notes, _fulfillment_type(raw), truck, driver])
    return rows


def flatten_order(order) -> list[list]:
    raw = order.raw_json or {}
    # Export only real Zoho line items. UI separator rows or empty objects must
    # never become blank spreadsheet/PDF rows.
    source_items = raw.get("line_items") or []
    items = [item for item in source_items if isinstance(item, dict) and any(item.get(key) not in (None, "") for key in ("name", "item_description", "sku", "quantity", "unit", "item_total", "amount"))]
    shipping = _address(raw.get("shipping_address") or order.shipping_address)
    address_value = raw.get("shipping_address") or order.shipping_address
    city = _text(shipping_city(order))
    invoice_status = _text(getattr(order, "invoice_status", "") or raw.get("invoice_status") or raw.get("invoiced_status")).lower()
    payment_status = _text(getattr(order, "payment_status", "") or raw.get("payment_status") or raw.get("paid_status")).lower()
    shipment_status = _text(getattr(order, "shipment_status", "") or raw.get("shipment_status") or raw.get("shipping_status") or raw.get("status")).lower()
    invoices = raw.get("invoices") if isinstance(raw.get("invoices"), list) else []
    packages = raw.get("packages") if isinstance(raw.get("packages"), list) else []
    invoiced = "Yes" if any(item.get("is_invoiced") is True or (item.get("quantity") and item.get("quantity_invoiced", 0) >= item.get("quantity")) for item in items) or any(_text(invoice.get("status")).lower() not in {"draft", "void", "cancelled", "canceled"} for invoice in invoices) or "invoiced" in invoice_status or "billed" in invoice_status else "No"
    payment = "Yes" if "paid" in payment_status or "payment received" in payment_status or any(invoice.get("balance") is not None and float(invoice.get("balance") or 0) <= 0 for invoice in invoices) else "No"
    packed = "Yes" if any(item.get("quantity") and item.get("quantity_packed", 0) >= item.get("quantity") for item in items) or any(value in _text(package.get("status") or package.get("detailed_status")).lower() for package in packages for value in ("packed", "fulfilled", "shipped", "delivered")) or any(value in shipment_status for value in ("packed", "shipped", "delivered")) else "No"
    shipped = "Yes" if any(item.get("quantity") and item.get("quantity_shipped", 0) >= item.get("quantity") for item in items) or any(value in _text(package.get("status") or package.get("detailed_status")).lower() for package in packages for value in ("shipped", "fulfilled", "delivered")) or any(value in shipment_status for value in ("shipped", "delivered")) else "No"
    return [[_date(order.expected_shipment_date), order.salesorder_number, order.customer_name, item.get("name") or item.get("item_description"), item.get("sku"), item.get("quantity"), item.get("unit"), city, shipping, _fulfillment_type(raw), order.order_status, invoiced, payment, packed, shipped, item.get("item_total") or order.total] for item in items]

def _number_format(value) -> str:
    whole = isinstance(value, (int, float)) and float(value).is_integer()
    return "#,##0;[Red]-#,##0" if whole else "#,##0.00;[Red]-#,##0.00"


def _make_confirmed_excel(rows: list[list]) -> BytesIO:
    book = Workbook(); sheet = book.active; sheet.title = "Sales Orders"
    thin = Side(style="thin", color="000000"); border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for column, header in enumerate(CONFIRMED_HEADERS, 1):
        cell = sheet.cell(1, column, header); cell.font = Font(bold=True, color="000000"); cell.fill = PatternFill("solid", fgColor="00FFFF"); cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True); cell.border = border
    sheet.row_dimensions[1].height = 48
    for row_number, row in enumerate(rows, 2):
        for column, value in enumerate(row, 1):
            cell = sheet.cell(row_number, column, value); cell.alignment = Alignment(vertical="center", wrap_text=column in (9, 13)); cell.border = border
            if isinstance(value, (int, float)) and column in (4, 11, 12): cell.number_format = _number_format(value)
    sheet.freeze_panes = "A2"
    widths = [13, 30, 14, 11, 8, 15, 28, 14, 48, 20, 16, 16, 60, 18, 14, 22]
    for column, width in enumerate(widths, 1): sheet.column_dimensions[get_column_letter(column)].width = width
    output = BytesIO(); book.save(output); output.seek(0); return output


def make_excel(rows: list[list], caption: str, layout: str = "default") -> BytesIO:
    if layout == "confirmed": return _make_confirmed_excel(rows)
    book = Workbook(); sheet = book.active; sheet.title = "Sales Orders"
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS)); sheet.cell(1, 1, "Rare Global Food Trading Corp.").font = Font(bold=True, size=16)
    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(HEADERS)); sheet.cell(2, 1, caption)
    for column, header in enumerate(HEADERS, 1):
        cell = sheet.cell(4, column, header); cell.font = Font(bold=True, color="FFFFFF"); cell.fill = PatternFill("solid", fgColor="111111")
    for row in rows: sheet.append(row)
    sheet.freeze_panes = "A5"
    for column in range(1, len(HEADERS) + 1): sheet.column_dimensions[get_column_letter(column)].width = min(34, max(12, max(len(_text(sheet.cell(row, column).value)) for row in range(4, sheet.max_row + 1)) + 2))
    output = BytesIO(); book.save(output); output.seek(0); return output

def make_pdf(rows: list[list], caption: str, layout: str = "default") -> BytesIO:
    output = BytesIO(); doc = SimpleDocTemplate(output, pagesize=landscape(A3), rightMargin=0.35 * inch, leftMargin=0.35 * inch, topMargin=0.35 * inch, bottomMargin=0.35 * inch)
    styles = getSampleStyleSheet(); title = ParagraphStyle("ExportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=RARE_RED); heading = ParagraphStyle("ExportHeading", parent=styles["Heading2"], fontName="Helvetica", fontSize=13, leading=16, textColor=BRAND_BLACK); caption_style = ParagraphStyle("ExportCaption", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=11, textColor=BRAND_BLACK)
    # Paragraph backgrounds must stay unset: ReportLab would paint them only
    # behind the text bounding box. TableStyle owns all cell/row backgrounds.
    cell_style = ParagraphStyle("ExportCell", parent=styles["Normal"], fontName="Helvetica", fontSize=8, leading=9, textColor=BRAND_BLACK)
    header_style = ParagraphStyle("ExportHeader", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=9, textColor=BRAND_WHITE)
    story = [Paragraph("<b>Rare Global Food Trading Corp.</b>", title), Paragraph("Sales Orders - Load Planning", heading), Paragraph(escape(caption), caption_style), Spacer(1, 0.15 * inch)]
    # A3 landscape gives the complete operational export room for all columns.
    confirmed = layout == "confirmed"
    widths = CONFIRMED_WIDTHS if confirmed else [52, 70, 95, 105, 55, 40, 35, 55, 145, 65, 60, 45, 45, 42, 42, 65]
    headers = CONFIRMED_HEADERS if confirmed else HEADERS
    confirmed_header_style = header_style.clone("ConfirmedHeader", textColor=BRAND_BLACK, alignment=1)
    negative_style = cell_style.clone("NegativeStock", textColor=NEGATIVE_RED, fontName="Helvetica-Bold")
    data = [[Paragraph(escape(header), confirmed_header_style if confirmed else header_style) for header in headers]]
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            style = cell_style
            if confirmed and index in CONFIRMED_STOCK_COLUMNS and isinstance(value, (int, float)):
                text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
                cells.append(Paragraph(escape(text), negative_style if value < 0 else style))
                continue
            if not confirmed and index == 10:
                style = cell_style.clone(f"status-{_text(value)}")
                style.textColor = _status_color(value)
            cells.append(Paragraph(escape(_text(value)), style))
        data.append(cells)
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), CYAN if confirmed else RARE_RED), ("TEXTCOLOR", (0, 0), (-1, 0), BRAND_BLACK if confirmed else BRAND_WHITE), ("TEXTCOLOR", (0, 1), (-1, -1), BRAND_BLACK), ("BACKGROUND", (0, 1), (-1, -1), BRAND_WHITE), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("GRID", (0, 0), (-1, -1), 0.25, BRAND_BLACK if confirmed else BEIGE), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [BRAND_WHITE] if confirmed else [BRAND_WHITE, BEIGE])]))
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
