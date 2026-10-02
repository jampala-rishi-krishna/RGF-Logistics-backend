from __future__ import annotations

from datetime import datetime, timezone
import os
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from html import escape

import httpx

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
from database import get_db
from types import SimpleNamespace
from models.dispatch_pipeline import ClientDeliveryConstraint
from models.vehicle import Vehicle
from models.inventory import SalesOrderCache
from services import optimizer
from services.rarechain_email_template import render_rarechain_email
from services import gmail_sender
from services.item_weight import calculate_order_weight_kg
from services.delivery_status import is_delivered
from services import staff_directory_cache, live_sales_order_cache, memory_tables, vapi_client, voice_calls
from services.sales_order_history_sync import sync_history_row
from routers.fleet import invalidate_fleet_cache
from routers.dispatch import normalize_ph_phone

router = APIRouter(prefix="/api/load-planning/assignments", tags=["assignments"], dependencies=[Depends(require_role("admin", "dispatcher", "warehouse"))])
optimize_router = APIRouter(prefix="/api/load-planning/assignments", tags=["assignments"], dependencies=[Depends(require_role("admin", "dispatcher", "warehouse"))])
LOCKED_VEHICLES = {"NAN9911": "Assigned to Production", "NAJ6018": "For Repair"}
# Only these roles are eligible to be assigned as a driver for a dispatch. Anyone
# else in the n8n staff directory (office staff, etc.) is still a valid "active"
# record but must not show up in the assignment picker.
ASSIGNABLE_DRIVER_TITLES = {"MC RIDER", "DELIVERY DRIVER", "DELIVERY HELPER"}
logger = logging.getLogger("assignment_notifications")
_notification_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="assignment-webhook")
# The frontend polls GET /{salesorder_id} every 20s per open assignment panel. That
# endpoint alone did 4 DB queries/tick with zero caching. A short TTL (< the 20s poll
# interval) keyed by salesorder_id, invalidated by assign_order below, keeps it out of
# Neon on every tick without ever serving genuinely stale vehicle/capacity data.
_ASSIGNMENT_OPTIONS_TTL_SECONDS = 15
_assignment_options_cache: dict[str, tuple[float, dict]] = {}


def _invalidate_assignment_options_cache() -> None:
    _assignment_options_cache.clear()
_NOTIFICATION_WEBHOOKS = (
    "https://rareglobalfood.app.n8n.cloud/webhook/intellifleet-logistics-driver-assignment",
    "https://rareglobalfood.app.n8n.cloud/webhook/intellifleet-logistics-whatsapp-initial",
    "https://rareglobalfood.app.n8n.cloud/webhook/intellifleet-logistics-sms-initial",
    "https://rareglobalfood.app.n8n.cloud/webhook/intellifleet-logistics-voice-call",
)
# Skipped when VOICE_PROVIDER=direct (services/voice_calls.py places the call instead).
_VOICE_WEBHOOK = _NOTIFICATION_WEBHOOKS[-1]
# Assignment emails (driver + team confirmation) go directly through the Gmail API when it is
# configured (services/gmail_sender.py), so this n8n webhook is skipped. WhatsApp/SMS still
# use their own n8n webhooks.
_EMAIL_WEBHOOK = _NOTIFICATION_WEBHOOKS[0]


def _send_assignment_emails_direct(driver, driver_subject: str, driver_html: str, team_subject: str | None, team_html: str | None) -> None:
    """Driver assignment email (and, once per assignment, the team confirmation) straight
    through Gmail. Runs in the notification pool; every outcome is logged by gmail_sender."""
    if driver.email:
        try:
            gmail_sender.send_email(to=driver.email, subject=driver_subject, html=driver_html, purpose="assignment-driver")
        except gmail_sender.GmailSendError:
            pass  # already logged with the reason
    else:
        logger.warning("[GMAIL_SEND] assignment email skipped: driver %s has no email on file", driver.name)
    if team_subject and team_html:
        team = [member.get("email") for member in staff_directory_cache.notify_list() if member.get("email")]
        if not team:
            logger.warning("[GMAIL_SEND] team confirmation skipped: the team notify list has no email addresses")
            return
        try:
            gmail_sender.send_email(to=team, subject=team_subject, html=team_html, purpose="assignment-team")
        except gmail_sender.GmailSendError:
            pass


def _send_notification(url: str, secret: str, payload: dict) -> None:
    try:
        response = httpx.post(url, headers={"Authorization": secret}, json=payload, timeout=30)
        response.raise_for_status()
        logger.info("Assignment notification dispatched channel=%s", url.rsplit("/", 1)[-1])
    except Exception as exc:
        logger.error("Assignment notification failed channel=%s error=%s", url.rsplit("/", 1)[-1], exc)


class AssignmentBody(BaseModel):
    salesorder_ids: list[str] = []
    vehicle_id: str
    driver_id: int | None = None
    # Multi-select driver assignment: every id here gets notified (email/WhatsApp/
    # SMS/voice) on send-assignment-email. The sales-order-history/live-cache
    # `driver_id` column only stores one integer, so persistence still records a
    # single "primary" driver (the first id) — see assign_order() below.
    driver_ids: list[int] = []

    def all_driver_ids(self) -> list[int]:
        if self.driver_ids:
            return list(dict.fromkeys(self.driver_ids))
        if self.driver_id is not None:
            return [self.driver_id]
        return []


class AssignmentEmailBody(AssignmentBody):
    assigned_by: str | None = None
    preview: bool = False
    html_body: str | None = None
    subject: str | None = None


def _assignment_email_html(drivers: list, profile, orders, assigned_by: str, assigned_at: str) -> tuple[str, str, str]:
    primary = drivers[0]
    driver_names = ", ".join(d.name for d in drivers if d.name) or "Driver"
    warehouse = {"METS": "Mets Cold Storage", "GLACIER": "Glacier Cold Storage"}.get(str(primary.warehouse or "").upper(), primary.warehouse or profile.capacity_note or "-")
    profile.capacity_note = warehouse
    subject = f"Driver assignment — {profile.plate_no or 'truck'} — {len(orders)} sales order(s)"
    rows = []
    for order in orders:
        raw = order.raw_json or {}
        address = raw.get("shipping_address") or order.shipping_address or {}
        if isinstance(address, list): address = address[0] if address else {}
        address_text = ", ".join(str(address.get(key)) for key in ("address", "street_address", "city", "state", "zip", "country") if isinstance(address, dict) and address.get(key))
        packs = sum(float(item.get("quantity") or 0) for item in raw.get("line_items", []))
        rows.append(f"<tr><td>{escape(str(order.salesorder_number or order.id))}</td><td>{escape(str(order.customer_name or '-'))}</td><td>{_weight(order):,.1f}</td><td>{packs:,.0f}</td><td>{escape(address_text or '-')}</td></tr>")
    headers = "".join(f"<th style='border:1px solid #ccc;padding:6px;text-align:left'>{heading}</th>" for heading in ("SO number", "Client", "Total kg", "Total Packs", "Shipping Address"))
    body_html = f"<p style='margin:0 0 16px;'>Please review the assigned sales orders below before dispatch and reply to confirm receipt.</p><table style='border-collapse:collapse;width:100%;font-size:12px;'><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    html_body = render_rarechain_email("COLD-CHAIN OPERATIONS / PHILIPPINES", "New dispatch assignment.", f"{driver_names} {'have' if len(drivers) > 1 else 'has'} been assigned to truck {profile.plate_no or '-'}.", "https://images.pexels.com/photos/7464230/pexels-photo-7464230.jpeg?auto=compress&amp;cs=tinysrgb&amp;w=1200", [{"label": "TRUCK PLATE", "value": profile.plate_no or "-"}, {"label": "WAREHOUSE PICKUP", "value": profile.capacity_note or "-"}, {"label": "TOTAL SOs", "value": str(len(orders))}], "https://images.pexels.com/photos/6169056/pexels-photo-6169056.jpeg?auto=compress&amp;cs=tinysrgb&amp;w=1800", "Route details for the assigned driver.", body_html)
    return subject, html_body, body_html


def _weight(order: SalesOrderCache) -> float:
    weight = calculate_order_weight_kg(order)
    # Missing item/package metadata must not prevent truck assignment. Treat the
    # unknown contribution as zero here; the authoritative displayed weight can
    # still be refreshed when Zoho metadata becomes available.
    return weight if weight is not None else 0.0


def _weight_if_known(order: SalesOrderCache) -> float | None:
    """Return authoritative weight when available without blocking assignment."""
    return calculate_order_weight_kg(order)


def _reefer(order: SalesOrderCache) -> bool:
    raw = order.raw_json or {}
    text = " ".join(str(raw.get(k) or "") for k in ("notes", "customer_name", "mode_of_transport")).lower()
    return any(term in text for term in ("reefer", "frozen", "chilled", "cold chain", "cold-chain"))


class NewDriverBody(BaseModel):
    name: str
    email: str
    phone: str
    title: str | None = None
    warehouse: str | None = None


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# Registered before "/{salesorder_id}" so "new-driver" isn't captured as a sales order id.
@router.post("/new-driver", status_code=201)
def create_new_driver(body: NewDriverBody, current_user: CurrentUser = Depends(require_role("admin", "dispatcher"))):
    """'+ New Driver' in the assignment panel. Writes to the n8n Staff Directory (the only
    source of truth for staff - never Neon) and returns the row, already in the in-memory
    cache, so the caller can assign with the returned id immediately."""
    name, email, phone = body.name.strip(), body.email.strip().lower(), body.phone.strip()
    missing = [label for label, value in (("Name", name), ("Email", email), ("Mobile Number", phone)) if not value]
    if missing:
        raise HTTPException(400, f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required.")
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "Email address is not valid.")
    phone_digits = re.sub(r"\D", "", phone)
    if not 10 <= len(phone_digits) <= 13:
        raise HTTPException(400, "Mobile Number must be a valid PH number, e.g. 09171234567.")
    title = (body.title or "DELIVERY DRIVER").strip().upper()
    if title not in ASSIGNABLE_DRIVER_TITLES:
        raise HTTPException(400, f"Title must be one of: {', '.join(sorted(ASSIGNABLE_DRIVER_TITLES))}.")
    existing = staff_directory_cache.find_by_contact(email, phone_digits)
    if existing:
        raise HTTPException(409, f"{existing.get('name')} already uses this email or mobile number - select them from the driver list instead.")
    try:
        staff = staff_directory_cache.create(name=name, email=email, phone=phone, title=title, warehouse=(body.warehouse or "").strip().upper() or None)
    except staff_directory_cache.StaffCreateError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    _invalidate_assignment_options_cache()
    logger.info("New driver created by %s: id=%s name=%s", current_user.email, staff.get("id"), staff.get("name"))
    return {"id": staff.get("id"), "name": staff.get("name"), "title": staff.get("title"), "email": staff.get("email"), "phone": staff.get("phone"), "warehouse": staff.get("warehouse")}


@router.get("/{salesorder_id}")
def assignment_options(salesorder_id: str, db: Session = Depends(get_db)):
    now = time.monotonic()
    cached = _assignment_options_cache.get(salesorder_id)
    if cached and now - cached[0] < _ASSIGNMENT_OPTIONS_TTL_SECONDS:
        return cached[1]

    order = live_sales_order_cache.find_cached(salesorder_id) or live_sales_order_cache.ensure_zoho_data(salesorder_id)
    if not order:
        raise HTTPException(404, "Sales order was not found.")
    profiles = db.execute(select(Vehicle).order_by(Vehicle.is_third_party, Vehicle.plate_no)).scalars().all()
    assigned = {}
    for existing in live_sales_order_cache.get_assigned_snapshot():
        # A truck's load is per delivery day: only orders for the same expected shipment date
        # count against its remaining capacity for this order.
        if existing.assignment_status == "assigned" and not is_delivered(existing.raw_json or {}) and existing.expected_shipment_date == order.expected_shipment_date:
            assigned[existing.vehicle_id] = assigned.get(existing.vehicle_id, 0.0) + _weight(existing)
    constraint = db.execute(select(ClientDeliveryConstraint).where(ClientDeliveryConstraint.customer_name == order.customer_name)).scalar_one_or_none()
    drivers = sorted((d for d in staff_directory_cache.all_staff() if d.get("active") and str(d.get("title") or "").strip().upper() in ASSIGNABLE_DRIVER_TITLES), key=lambda d: d.get("name") or "")
    weight = _weight_if_known(order)
    result = {"order": {"id": order.id, "number": order.salesorder_number, "customer": order.customer_name, "address": (order.raw_json or {}).get("shipping_address"), "weight_kg": weight, "weight_verified": weight is not None, "weight_warning": None if weight is not None else "Zoho package weight unavailable; assignment is allowed but capacity remains unverified.", "requires_reefer": _reefer(order), "assignment_status": order.assignment_status}, "constraint": constraint and {"opening_time": constraint.opening_time, "receiving_cutoff_time": constraint.receiving_cutoff_time, "avg_processing_time_minutes": constraint.avg_processing_time_minutes, "requires_reefer": constraint.requires_reefer, "notes": constraint.notes}, "constraint_status": "on file" if constraint else "No delivery constraints on file", "vehicles": [{"vehicle_id": p.plate_no, "vehicle_type": p.vehicle_type, "capacity_kg": p.rated_capacity_kg, "capacity_note": p.capacity_note, "reefer": p.is_reefer, "gps_tracked": p.is_gps_tracked, "third_party": p.is_third_party, "locked": str(p.plate_no or "").upper() in LOCKED_VEHICLES, "lock_reason": LOCKED_VEHICLES.get(str(p.plate_no or "").upper()), "assigned_weight_kg": float(assigned.get(p.plate_no, 0) or 0), "remaining_capacity_kg": None if p.rated_capacity_kg is None else max(0, float(p.rated_capacity_kg) - float(assigned.get(p.plate_no, 0) or 0))} for p in profiles if str(p.plate_no or "").upper() not in LOCKED_VEHICLES], "drivers": [{"id": d.get("id"), "name": d.get("name"), "title": d.get("title")} for d in drivers]}
    _assignment_options_cache[salesorder_id] = (now, result)
    return result


@router.post("/send-assignment-email")
def send_assignment_email_route(body: AssignmentEmailBody, current_user: CurrentUser = Depends(require_role("admin", "dispatcher")), db: Session = Depends(get_db)):
    return send_assignment_email(body, current_user, db)


@router.post("/{salesorder_id}")
def assign_order(salesorder_id: str, body: AssignmentBody, current_user: CurrentUser = Depends(require_role("admin", "dispatcher")), db: Session = Depends(get_db)):
    order_ids = body.salesorder_ids or [salesorder_id]
    orders = [o for o in (live_sales_order_cache.find_cached(oid) or live_sales_order_cache.ensure_zoho_data(oid) for oid in order_ids) if o is not None]
    order = live_sales_order_cache.find_cached(salesorder_id) or live_sales_order_cache.ensure_zoho_data(salesorder_id)
    profile = db.execute(select(Vehicle).where(Vehicle.plate_no == body.vehicle_id)).scalar_one_or_none()
    if not order or not profile or len(orders) != len(set(order_ids)):
        raise HTTPException(404, "Sales order or vehicle was not found.")
    if str(profile.plate_no or "").upper() in LOCKED_VEHICLES:
        raise HTTPException(409, f"Vehicle unavailable: {LOCKED_VEHICLES[str(profile.plate_no).upper()]}")
    if any(item.assignment_status == "assigned" for item in orders):
        raise HTTPException(409, "One or more sales orders are already assigned.")
    known_weights = [_weight_if_known(item) for item in orders]
    # Capacity is checked per delivery day: the 5th's orders never use up the truck's room on
    # the 3rd, and each day in this request is checked against only that day's existing load.
    snapshot = live_sales_order_cache.get_assigned_snapshot()
    existing_weights: list[float | None] = []
    for ship_date in {item.expected_shipment_date for item in orders}:
        day_known = [_weight_if_known(item) for item in orders if item.expected_shipment_date == ship_date]
        day_existing = [_weight_if_known(existing) for existing in snapshot if existing.vehicle_id == body.vehicle_id and existing.assignment_status == "assigned" and not is_delivered(existing.raw_json or {}) and existing.expected_shipment_date == ship_date]
        existing_weights.extend(day_existing)
        day_new_weight = sum(value or 0 for value in day_known)
        day_assigned = sum(value or 0 for value in day_existing)
        if profile.rated_capacity_kg is not None and all(value is not None for value in day_known + day_existing) and float(day_assigned) + day_new_weight > float(profile.rated_capacity_kg):
            raise HTTPException(409, f"Capacity exceeded for {ship_date}: {day_new_weight + float(day_assigned):.1f} kg requested, {float(profile.rated_capacity_kg):.1f} kg available.")
    if any(_reefer(item) for item in orders) and profile.is_reefer is False:
        raise HTTPException(409, "This order requires a reefer-capable vehicle.")
    driver_ids = body.all_driver_ids()
    for driver_id in driver_ids:
        if not staff_directory_cache.get_by_id(driver_id):
            raise HTTPException(404, "Driver was not found.")
    # sales_order_history / the live cache only have a single `driver_id` column,
    # so the first selected driver is persisted as the "primary" driver. Every id
    # in driver_ids still gets the assignment notifications (see
    # send_assignment_email below) regardless of this DB limitation.
    primary_driver_id = driver_ids[0] if driver_ids else None
    assigned_at = datetime.now(timezone.utc)
    for item in orders:
        item.vehicle_id = body.vehicle_id
        item.driver_id = primary_driver_id
        item.helper_ids = driver_ids
        item.assigned_at = assigned_at
        item.assigned_by = current_user.id
        item.assignment_status = "assigned"
        live_sales_order_cache.set_assignment(item.id, vehicle_id=body.vehicle_id, driver_id=primary_driver_id, assigned_at=assigned_at, assigned_by=current_user.id, assignment_status="assigned")
        sync_history_row(db, item)
    db.commit()
    invalidate_fleet_cache()
    _invalidate_assignment_options_cache()
    return {"success": True, "salesorder_ids": [item.id for item in orders], "vehicle_id": body.vehicle_id, "driver_id": primary_driver_id, "driver_ids": driver_ids, "assignment_status": "assigned", "capacity_verified": all(value is not None for value in known_weights + existing_weights), "capacity_warning": None if all(value is not None for value in known_weights + existing_weights) else "Assignment completed, but one or more Zoho package weights were unavailable; capacity must be verified before dispatch."}


@router.post("/{salesorder_id}/unassign")
def unassign_order(salesorder_id: str, current_user: CurrentUser = Depends(require_role("admin", "dispatcher")), db: Session = Depends(get_db)):
    order = live_sales_order_cache.find_cached(salesorder_id) or live_sales_order_cache.ensure_zoho_data(salesorder_id)
    if order is None:
        raise HTTPException(404, "Sales order was not found.")
    if order.assignment_status in {"completed", "delivered"} or is_delivered(order.raw_json or {}):
        raise HTTPException(409, "Delivered or completed sales orders cannot be unassigned.")
    if order.manifest_id is not None or order.assignment_status == "manifested":
        raise HTTPException(409, "Sales orders on a confirmed manifest cannot be unassigned.")
    order.vehicle_id = None
    order.driver_id = None
    order.route_id = None
    order.manifest_id = None
    order.assigned_at = None
    order.assigned_by = None
    order.assignment_status = "unassigned"
    live_sales_order_cache.set_assignment(
        order.id,
        vehicle_id=None,
        driver_id=None,
        route_id=None,
        manifest_id=None,
        assigned_at=None,
        assigned_by=None,
        assignment_status="unassigned",
    )
    sync_history_row(db, order)
    db.commit()
    invalidate_fleet_cache()
    live_sales_order_cache.invalidate_assigned_zoho()
    live_sales_order_cache.invalidate_windows()
    _invalidate_assignment_options_cache()
    return {"success": True, "salesorder_id": order.id, "assignment_status": "unassigned"}


@optimize_router.post("/vehicle/{vehicle_id}/optimize-stops")
def optimize_assigned_stops(vehicle_id: str, current_user: CurrentUser = Depends(require_role("admin", "dispatcher")), db: Session = Depends(get_db)):
    orders = sorted((o for o in live_sales_order_cache.get_assigned_snapshot() if o.vehicle_id == vehicle_id and o.assignment_status == "assigned"), key=lambda o: o.assigned_at or datetime.min.replace(tzinfo=timezone.utc))
    if len(orders) < 2:
        raise HTTPException(409, "Optimize stops requires at least two assigned orders on this truck.")
    size = len(orders) + 1
    matrix = [[0.0 if i == j else 1.0 for j in range(size)] for i in range(size)]
    result = optimizer.solve(optimizer.OptimizeRequest(vehicles=[optimizer.Vehicle(id=vehicle_id, capacity_kg=1e9, start_node=0, end_node=0, shift_start=0, shift_end=1440, temperature_capabilities=["ambient", "chilled", "frozen"])], shipments=[optimizer.Shipment(id=o.id, node=i + 1, demand_kg=_weight(o), service_time_min=15, time_window_start=0, time_window_end=1440, temperature_requirement="ambient", priority=1) for i, o in enumerate(orders)], distance_matrix_km=matrix, duration_matrix_min=matrix, objective="recommended"))
    if not result.feasible or not result.routes or len(result.routes[0].stop_sequence) != len(orders):
        raise HTTPException(422, "The Fleet Optimization engine could not produce a feasible stop order.")
    route = memory_tables.routes.create(name=f"Optimized stops · {vehicle_id}", mode="recommended", status="optimized", distance_km=result.routes[0].total_distance_km, duration_min=result.routes[0].total_duration_min)
    by_id = {o.id: o for o in orders}
    for sequence, order_id in enumerate(result.routes[0].stop_sequence, 1):
        order = by_id[str(order_id)]
        order.route_id = str(route["id"])
        live_sales_order_cache.set_assignment(order.id, route_id=str(route["id"]))
        sync_history_row(db, order)
        memory_tables.route_stops.create(route_id=route["id"], sequence=sequence, location_name=order.customer_name or order.salesorder_number, lat=None, lng=None)
    db.commit()
    return {"success": True, "route_id": str(route["id"]), "vehicle_id": vehicle_id, "stop_sequence": result.routes[0].stop_sequence, "orders": [{"id": o.id, "salesorder_number": o.salesorder_number, "route_id": o.route_id} for o in orders]}


@router.post("/send-assignment-email-internal")
def send_assignment_email(body: AssignmentEmailBody, current_user: CurrentUser = Depends(require_role("admin", "dispatcher")), db: Session = Depends(get_db)):
    secret = os.environ.get("INTELLIFLEET_ASSIGNMENT_WEBHOOK_SECRET", "").strip()
    if not secret and not body.preview:
        raise HTTPException(503, "Assignment webhook secret is not configured.")
    ids = body.salesorder_ids
    if not ids:
        raise HTTPException(400, "At least one sales order is required.")
    orders = [o for o in (live_sales_order_cache.find_cached(oid) or live_sales_order_cache.ensure_zoho_data(oid) for oid in ids) if o is not None]
    driver_ids = body.all_driver_ids()
    driver_rows = [staff_directory_cache.get_by_id(driver_id) for driver_id in driver_ids]
    if not driver_ids and profile and profile.is_third_party:
        return {"skipped": True, "reason": "Third-party vehicle has no staff recipient."}
    if not driver_ids or any(row is None for row in driver_rows):
        raise HTTPException(404, "Driver was not found.")
    drivers = [SimpleNamespace(**row) for row in driver_rows]
    profile = db.execute(select(Vehicle).where(Vehicle.plate_no == body.vehicle_id)).scalar_one_or_none()
    if len(orders) != len(set(ids)) or not profile:
        raise HTTPException(404, "Sales order or vehicle was not found.")
    def address(order):
        value = (order.raw_json or {}).get("shipping_address") or order.shipping_address or {}
        if isinstance(value, list): value = value[0] if value else {}
        return ", ".join(str(value.get(key)) for key in ("address", "street_address", "city", "state", "zip", "country") if isinstance(value, dict) and value.get(key))
    def packs(order): return sum(float(item.get("quantity") or 0) for item in (order.raw_json or {}).get("line_items", []))
    assigned_by = body.assigned_by or current_user.full_name or current_user.email
    assigned_at = datetime.now(timezone.utc).isoformat()
    driver_subject, generated_driver_html, body_html = _assignment_email_html(drivers, profile, orders, assigned_by, assigned_at)
    primary = drivers[0]
    driver_names = ", ".join(d.name for d in drivers if d.name) or "Driver"
    warehouse = {"METS": "Mets Cold Storage", "GLACIER": "Glacier Cold Storage"}.get(str(primary.warehouse or "").upper(), primary.warehouse or profile.capacity_note or "-")
    team_subject = f"Assignment Confirmed: {driver_names} - {profile.plate_no or '-'}"
    team_html = render_rarechain_email("COLD-CHAIN OPERATIONS / PHILIPPINES", "Assignment confirmed and dispatched.", f"{driver_names} {'have' if len(drivers) > 1 else 'has'} been assigned to truck {profile.plate_no or '-'} for {len(orders)} sales order(s).", "https://images.pexels.com/photos/7464230/pexels-photo-7464230.jpeg?auto=compress&amp;cs=tinysrgb&amp;w=1200", [{"label": "TRUCK PLATE", "value": profile.plate_no or "-"}, {"label": "DRIVER(S)" if len(drivers) > 1 else "DRIVER", "value": driver_names}, {"label": "WAREHOUSE", "value": warehouse}], "https://images.pexels.com/photos/6169056/pexels-photo-6169056.jpeg?auto=compress&amp;cs=tinysrgb&amp;w=1800", "Route details for your records.", f"<p style='margin:0 0 16px;'>Hi {{{{RECIPIENT_FIRST_NAME}}}},</p>{body_html}")
    if body.preview:
        return {"driverHtmlBody": generated_driver_html, "driverSubject": driver_subject, "teamHtmlBody": team_html, "teamSubject": team_subject}
    driver_html = body.html_body or generated_driver_html
    sales_orders_payload = [{"soNumber": order.salesorder_number, "clientName": order.customer_name, "totalKgs": _weight(order), "totalPacks": packs(order), "shippingAddress": address(order)} for order in orders]
    # Every selected driver gets their own notification pass across all four
    # channels (email/whatsapp/sms/voice), each addressed to that driver's own
    # name/email/phone — never a mix-up with another selected driver's details.
    # The team confirmation email is only attached to the first driver's payload
    # so it doesn't fire once per driver.
    voice_direct = vapi_client.voice_provider() == "direct"
    for index, driver in enumerate(drivers):
        payload = {
            "driverName": driver.name,
            "driverEmail": driver.email,
            "driverPhone": normalize_ph_phone(driver.phone) or driver.phone,
            "vehicleId": body.vehicle_id,
            "truckPlate": profile.plate_no,
            "warehouse": warehouse,
            "assignedBy": assigned_by,
            "assignedAt": assigned_at,
            "salesOrders": sales_orders_payload,
            "driverSubject": body.subject or driver_subject,
            "driverHtmlBody": driver_html,
            "teamSubject": team_subject if index == 0 else None,
            "teamHtmlBody": team_html if index == 0 else None,
        }
        email_direct = gmail_sender.configured()
        if email_direct:
            _notification_pool.submit(_send_assignment_emails_direct, driver, payload["driverSubject"], driver_html, payload["teamSubject"], payload["teamHtmlBody"])
        for webhook in _NOTIFICATION_WEBHOOKS:
            if voice_direct and webhook == _VOICE_WEBHOOK:
                continue
            if email_direct and webhook == _EMAIL_WEBHOOK:
                continue  # email goes directly through Gmail; WhatsApp/SMS still use n8n
            _notification_pool.submit(_send_notification, webhook, secret, payload)
    if voice_direct:
        # VOICE_PROVIDER=direct: one Vapi call per driver straight from here, replacing the
        # n8n voice-call webhook. The other three channels above are unchanged.
        _notification_pool.submit(
            voice_calls.place_assignment_calls_sync,
            assignment_id=f"asg-{uuid.uuid4().hex[:12]}",
            salesorder_ids=[order.id for order in orders],
            vehicle_id=body.vehicle_id,
            truck_plate=profile.plate_no,
            warehouse=warehouse,
            sales_orders=sales_orders_payload,
            drivers=driver_rows,
        )
    return {"success": True, "dispatched": ["email", "whatsapp", "sms", "voice"], "drivers": [d.name for d in drivers], "message": "Notifications dispatched asynchronously."}
