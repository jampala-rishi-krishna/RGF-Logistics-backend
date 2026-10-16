"""Pickup preview, printable attachments, daily scheduling and durable send outcomes."""
from datetime import datetime, timedelta
from hashlib import sha256
from html import escape
from io import BytesIO
import json
import threading
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from services import app_settings, gmail_sender, pickup_rules, purchase_orders
from models.access_workflow import PickupEmailLog

_send_lock = threading.Lock()
_last_tick = None


def recipients():
    split = lambda key: [v.strip() for v in app_settings.get_str(key).split(',') if v.strip()]
    return {"to": list(dict.fromkeys(split("PICKUP_PROCUREMENT_TO") + split("PICKUP_LOGISTICS_TO"))),
            "cc": list(dict.fromkeys(split("PICKUP_PROCUREMENT_CC") + split("PICKUP_LOGISTICS_CC")))}


def printable(order):
    summary = purchase_orders.summary(order)
    raw = order.raw_json or {}
    address = raw.get('shipping_address') or {}
    contact = raw.get('vendor_contact') or {}
    return {"po": order.salesorder_number, "vendor": order.customer_name,
        "address": ", ".join(str(address[k]) for k in ('address', 'street2', 'city', 'state', 'zip', 'country') if address.get(k)),
        "contact": f"{contact.get('person') or ''} {contact.get('phone') or ''}".strip(),
        "delivery_date": str(order.expected_shipment_date), "destination": order.destination_warehouse or '',
        "items": [{"name": p.get('name'), "qty": p.get('quantity'), "unit": p.get('unit'), "kg": p.get('total_weight_kg')} for p in summary.get('products', [])],
        "vendor_address_missing": raw.get('vendor_address_missing', True)}


def preview():
    day = datetime.now(pickup_rules.PHT).date() + timedelta(days=1)
    records, _ = purchase_orders.get_records()
    scheduled, pahabol = [], []
    for record in records.values():
        if not purchase_orders.is_wanted(record) or pickup_rules.classify(record) == 'excluded':
            continue
        pid = str(record['purchaseorder_id'])
        detail = purchase_orders.cached_detail(pid, record) or purchase_orders.fetch_detail(pid, record)
        merged = {**record, **detail}
        state = purchase_orders.pickup_state(merged)
        if state in {'scheduled', 'pahabol'}:
            (scheduled if state == 'scheduled' else pahabol).append(printable(purchase_orders.build_row(record, detail)))
    scheduled.sort(key=lambda r: r['po'])
    pahabol.sort(key=lambda r: r['po'])
    data = {"delivery_date": day.isoformat(), "recipients": recipients(), "scheduled": scheduled, "pahabol": pahabol}
    data['preview_token'] = sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    return data


def documents(scheduled, pahabol):
    from openpyxl import Workbook
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    styles = getSampleStyleSheet()
    story = [Paragraph('Rare Global Food - Pickup list', styles['Title'])]
    book = Workbook()
    book.remove(book.active)
    for title, rows in [('Scheduled pickups', scheduled), ('Not scheduled - needs Logistics approval', pahabol)]:
        story.extend([Spacer(1, 12), Paragraph(title, styles['Heading2'])])
        sheet = book.create_sheet('Scheduled' if title == 'Scheduled pickups' else 'Pahabol - not scheduled')
        headers = ['PO#', 'Vendor', 'Pickup shipping address', 'Contact / phone', 'Delivery date', 'Destination warehouse', 'Item', 'Qty', 'Unit', 'kg']
        sheet.append(headers)
        sheet.freeze_panes = 'A2'
        for row in rows:
            warning = ' - Vendor address missing' if row.get('vendor_address_missing') else ''
            story.append(Paragraph(escape(f"{row['po']} | {row['vendor']}{warning}"), styles['Heading3']))
            story.append(Paragraph(escape(f"Pick up at {row['address'] or 'Vendor address missing'}. Contact: {row['contact'] or 'Missing'}. Deliver {row['delivery_date']} to {row['destination']}"), styles['BodyText']))
            cells = [['Item', 'Qty', 'Unit', 'kg']]
            for item in row['items'] or [{}]:
                values = [row['po'], row['vendor'], row['address'], row['contact'], row['delivery_date'], row['destination'], item.get('name') or '', item.get('qty'), item.get('unit'), item.get('kg')]
                sheet.append(values)
                # Treat all provider strings as text, including formula-like product/vendor names.
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = 's'
                cells.append([Paragraph(escape(str(item.get(k) if item.get(k) is not None else '—')), styles['BodyText']) for k in ('name','qty','unit','kg')])
            table = Table(cells, colWidths=[450, 70, 80, 80], repeatRows=1, hAlign='LEFT')
            table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e5e7eb')), ('GRID',(0,0),(-1,-1),0.4,colors.grey), ('VALIGN',(0,0),(-1,-1),'TOP')]))
            story.append(table)
        from openpyxl.styles import Alignment, Font
        from openpyxl.utils import get_column_letter
        for col, width in enumerate([23,30,55,30,17,30,45,12,12,12],1):
            sheet.column_dimensions[get_column_letter(col)].width = width
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in sheet:
            for cell in row:
                cell.alignment = Alignment(vertical='top', wrap_text=True)
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = 'landscape'
        sheet.page_setup.fitToWidth = 1
        sheet.print_title_rows = '1:1'
    pdf, excel = BytesIO(), BytesIO()
    SimpleDocTemplate(pdf, pagesize=landscape(A4), rightMargin=28, leftMargin=28).build(story)
    book.save(excel)
    return pdf.getvalue(), excel.getvalue()


def send(db, *, preview_token=None, schedule_key=None):
    with _send_lock:
        data = preview()
        if preview_token is not None and preview_token != data['preview_token']:
            raise ValueError('The pickup list or recipients changed. Preview again before sending.')
        count = len(data['scheduled']) + len(data['pahabol'])
        log = PickupEmailLog(delivery_date=datetime.fromisoformat(data['delivery_date']).date(), recipients=data['recipients'], po_count=count,
                             status='sending', schedule_key=schedule_key)
        db.add(log)
        try:
            db.commit()  # durable claim before external delivery; prevents another scheduler sending twice
        except IntegrityError:
            db.rollback()
            return {"status": "already_attempted"}
        if count == 0:
            log.status = 'skipped_empty'
        elif not data['recipients']['to']:
            log.status, log.error = 'failed', 'Pickup email recipients are not configured'
        else:
            try:
                pdf, excel = documents(data['scheduled'], data['pahabol'])
                html = '<h1>Pickup list</h1>'
                for title, rows in [('Scheduled pickups', data['scheduled']), ('Not scheduled – needs Logistics approval', data['pahabol'])]:
                    html += f'<h2>{title}</h2><ul>' + ''.join(f"<li>{escape(row['po'])}: {escape(row['vendor'])} — {escape(row['address'])}; {escape(row['contact'])}; {escape(row['delivery_date'])} → {escape(row['destination'])}; " + escape("; ".join(f"{item.get('name')} {item.get('qty')} {item.get('unit')} / {item.get('kg') if item.get('kg') is not None else 'Unknown'} kg" for item in row['items'])) + "</li>" for row in rows) + '</ul>'
                gmail_sender.send_email(to=data['recipients']['to'], cc=data['recipients']['cc'], subject=f"Pickup list — {data['delivery_date']}", html=html,
                    attachments=[('Pickup-list.pdf','application/pdf',pdf), ('Pickup-list.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',excel)], purpose='pickup-list')
                log.status = 'sent'
            except Exception as exc:
                log.status, log.error = 'failed', str(exc)[:1000]
        db.commit()
        return {"status": log.status, "po_count": count, "error": log.error}


def scheduled_tick(now=None):
    global _last_tick
    now = now or datetime.now(pickup_rules.PHT)
    if app_settings.get_int('PICKUP_EMAIL_ENABLED') != 1 or not recipients()['to']:
        return
    if now.strftime('%H:%M') != app_settings.get_str('PICKUP_EMAIL_TIME'):
        return
    key = now.date().isoformat()
    if _last_tick == key:
        return
    from database import SessionLocal
    with SessionLocal() as db:
        if db.execute(select(PickupEmailLog.id).where(PickupEmailLog.schedule_key == key)).first():
            _last_tick = key
            return
        try:
            send(db, schedule_key=key)
        except Exception as exc:
            db.rollback()
            db.add(PickupEmailLog(delivery_date=now.date()+timedelta(days=1), recipients=recipients(), po_count=0, status='failed', error=str(exc)[:1000], schedule_key=key))
            db.commit()
        _last_tick = key
