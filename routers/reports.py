from __future__ import annotations

import re
import time
import os
import contextvars
from copy import deepcopy
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from threading import Lock
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException

from auth.dependencies import get_current_user
from services import zoho_acquisition
from services.zoho_client import (
    ZohoError,
    fetch_inventory_adjustments,
    fetch_invoices,
    fetch_purchase_receive_detail,
    fetch_purchase_receives,
    fetch_sales_order_detail,
    fetch_sales_orders,
    fetch_packages,
    fetch_transfer_orders,
)

router = APIRouter(prefix="/api/reports", tags=["reports"], dependencies=[Depends(get_current_user)])

PHT = ZoneInfo("Asia/Manila")
_CACHE_TTL_SECONDS = 60
_DETAIL_CAP = 80
_cache_lock = Lock()
_cache: dict = {"payload": None, "fetched_at": 0.0}
_report_inflight: dict[tuple, Future] = {}

_TRANSFER_OPEN_STATUSES = {"draft", "pending_approval", "approved", "in_transit"}
_IA_WINDOW_DAYS = 90


def _today() -> date:
    return datetime.now(PHT).date()


def _pick(record: dict, *keys: str):
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _text(value) -> str:
    return str(value or "")


def _lower(value) -> str:
    return _text(value).lower()


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _page_oldest(records: list[dict], *date_keys: str) -> date | None:
    dates = [_as_date(_pick(record, *date_keys)) for record in records]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def _customer_name(record: dict) -> str | None:
    return _pick(record, "customer_name") or (record.get("customer") or {}).get("customer_name") or (record.get("customer") or {}).get("display_name")


def _custom_field(record: dict, api_name: str) -> str | None:
    for field in record.get("custom_fields") or []:
        if _lower(field.get("api_name")) == api_name or _lower(field.get("label")).replace(" ", "_") == api_name.removeprefix("cf_"):
            value = _pick(field, "value", "formatted_value")
            return _text(value) if value is not None else None
    return _pick(record, api_name)


def _fulfillment_type(record: dict) -> str | None:
    value = _custom_field(record, "cf_fulfillment_type")
    if value:
        return value
    if re.search(r"pick\s*-?\s*up", _text(record.get("delivery_method")), re.I):
        return "Pick-up by Customer"
    return "Company Delivery"


def _so_sarisuki(record: dict) -> bool:
    return "sarisuki" in _lower(record.get("branch_name")) or "sarisuki" in _lower(record.get("customer_name"))


def _package_sarisuki(record: dict) -> bool:
    return (
        re.search(r"^SS\s", _text(_pick(record, "salesorder_number", "sales_order_number")), re.I) is not None
        or "mcssi" in _lower(record.get("customer_name"))
        or "sarisuki" in _lower(record.get("delivery_method"))
    )


def _warehouse_name(record: dict) -> str:
    return (
        _pick(record, "warehouse_name", "location_name", "branch_name")
        or (record.get("warehouse") or {}).get("warehouse_name")
        or (record.get("location") or {}).get("location_name")
        or "Unassigned Warehouse"
    )


def _warehouse_tag(name: str | None) -> str:
    value = _text(name).strip()
    if re.search(r"glacier", value, re.I):
        return "GLA"
    if re.search(r"mets", value, re.I):
        return "METS"
    return value[:4].upper() if value else "UNAS"


def _detail_record(payload: dict, key: str) -> dict:
    return payload.get(key) or payload


def _sales_order_warehouses(detail: dict) -> list[str]:
    so = _detail_record(detail, "salesorder")
    names: list[str] = []
    for item in so.get("line_items") or []:
        name = _pick(item, "warehouse_name", "location_name") or (item.get("warehouse") or {}).get("warehouse_name")
        if name and name not in names:
            names.append(str(name))
    return names or [_warehouse_name(so)]


def _detail_map(ids: list[str], fetch, key: str, workers: int, errors: dict | None = None) -> dict[str, dict]:
    unique_ids = [item for i, item in enumerate(ids[:_DETAIL_CAP]) if item and item not in ids[:i]]
    results: dict[str, dict] = {}
    if not unique_ids:
        return results
    zoho_acquisition.event("report_candidates", resource=key, candidates=len(ids), selected=len(ids[:_DETAIL_CAP]), unique=len(unique_ids))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(contextvars.copy_context().run, fetch, item_id): item_id for item_id in unique_ids}
        for future in as_completed(futures):
            item_id = futures[future]
            try:
                results[item_id] = _detail_record(future.result(), key)
            except ZohoError as exc:
                if errors is not None:
                    errors.setdefault("detail_enrichment", str(exc))
    return results


def _paginate(fetch_fn, list_key: str, max_pages: int, per_page: int = 200, stop_when=None) -> list[dict]:
    records: list[dict] = []
    for page in range(1, max_pages + 1):
        payload = fetch_fn(page=page, per_page=per_page)
        page_records = payload.get(list_key) or []
        records.extend(page_records)
        if stop_when and stop_when(page_records):
            break
        context = payload.get("page_context") or {}
        if not context.get("has_more_page"):
            break
    return records


def _safe_fetch(resource: str, errors: dict, loader) -> list[dict]:
    try:
        return loader()
    except ZohoError as exc:
        message = str(exc)
        if resource == "inventory_adjustments" and re.search(r"not authorized|57", message, re.I):
            errors[resource] = "Your Zoho Inventory connector permissions do not currently allow reading inventory adjustments."
        else:
            errors[resource] = message
        return []


def _group_rows(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row.pop("_warehouse"), []).append(row)
    return [{"warehouse": name, "count": len(orders), "orders": orders} for name, orders in sorted(groups.items())]


def _base_so_row(so: dict, detail: dict | None = None) -> dict:
    source = detail or so
    return {
        "id": str(_pick(source, "salesorder_id", "sales_order_id", "id") or _pick(so, "salesorder_id", "sales_order_id", "id") or ""),
        "so_number": _pick(source, "salesorder_number", "sales_order_number") or _pick(so, "salesorder_number", "sales_order_number"),
        "customer": _customer_name(source) or _customer_name(so),
        "fulfillment_type": _fulfillment_type(source) or _fulfillment_type(so),
    }


def _build_due_not_packed(sales_orders: list[dict], today: date, errors: dict) -> list[dict]:
    rows: list[dict] = []
    candidates = []
    for so in sales_orders:
        sub_status = _lower(_pick(so, "current_sub_status", "order_sub_status"))
        due = _as_date(_pick(so, "shipment_date", "expected_shipment_date", "due_date"))
        shipped_status = _lower(_pick(so, "shipped_status", "shipment_status", "shipping_status"))
        if (
            not _so_sarisuki(so)
            and _lower(so.get("order_status")) == "confirmed"
            and (sub_status == "confirmed" or "ack" in sub_status)
            and float(_pick(so, "quantity_packed") or 0) == 0
            and shipped_status not in {"fulfilled", "shipped"}
            and due
            and due <= today
        ):
            candidates.append(so)
    details = _detail_map([str(_pick(so, "salesorder_id", "sales_order_id", "id") or "") for so in candidates], fetch_sales_order_detail, "salesorder", 5, errors)
    for so in candidates[:_DETAIL_CAP]:
        so_id = str(_pick(so, "salesorder_id", "sales_order_id", "id") or "")
        detail = details.get(so_id)
        due = _as_date(_pick(so, "shipment_date", "expected_shipment_date", "due_date"))
        for warehouse in _sales_order_warehouses(detail or so):
            rows.append({**_base_so_row(so, detail), "due_date": due, "_warehouse": warehouse})
    return sorted(rows, key=lambda row: (row.get("due_date") or date.max, _text(row.get("customer"))))


def _build_packed_not_shipped(packages: list[dict], today: date, errors: dict) -> list[dict]:
    deduped: dict[str, dict] = {}
    for pkg in packages:
        so_number = _text(_pick(pkg, "salesorder_number", "sales_order_number"))
        if _lower(pkg.get("status")) == "not_shipped" and not _package_sarisuki(pkg) and so_number and so_number not in deduped:
            deduped[so_number] = pkg
    candidates = list(deduped.values())[:_DETAIL_CAP]
    details = _detail_map([str(_pick(pkg, "salesorder_id", "sales_order_id") or "") for pkg in candidates], fetch_sales_order_detail, "salesorder", 3, errors)
    rows: list[dict] = []
    for pkg in candidates:
        so_id = str(_pick(pkg, "salesorder_id", "sales_order_id") or "")
        detail = details.get(so_id) or {}
        delivery_date = _as_date(_pick(detail, "shipment_date", "expected_shipment_date", "due_date"))
        if delivery_date and delivery_date > today:
            continue
        for warehouse in _sales_order_warehouses(detail or pkg):
            rows.append(
                {
                    **_base_so_row(pkg, detail),
                    "id": so_id or str(_pick(pkg, "package_id", "id") or ""),
                    "package_id": str(_pick(pkg, "package_id", "id") or ""),
                    "delivery_date": delivery_date,
                    "_warehouse": warehouse,
                }
            )
    return rows


def _build_shipped_not_delivered(packages: list[dict], errors: dict) -> list[dict]:
    candidates = [pkg for pkg in packages if _lower(pkg.get("status")) == "shipped" and not _package_sarisuki(pkg)][:_DETAIL_CAP]
    details = _detail_map([str(_pick(pkg, "salesorder_id", "sales_order_id") or "") for pkg in candidates], fetch_sales_order_detail, "salesorder", 3, errors)
    rows: list[dict] = []
    for pkg in candidates:
        so_id = str(_pick(pkg, "salesorder_id", "sales_order_id") or "")
        detail = details.get(so_id) or {}
        rows.append(
            {
                **_base_so_row(pkg, detail),
                "id": so_id,
                "package_id": str(_pick(pkg, "package_id", "id") or ""),
                "delivery_date": _as_date(_pick(detail, "shipment_date", "expected_shipment_date", "due_date")),
                "shipped_date": _as_date(pkg.get("shipment_date")),
            }
        )
    return rows


def _build_delivered_today(packages: list[dict], today: date) -> list[dict]:
    return [
        {
            "id": str(_pick(pkg, "salesorder_id", "sales_order_id") or ""),
            "package_id": str(_pick(pkg, "package_id", "id") or ""),
            "so_number": _pick(pkg, "salesorder_number", "sales_order_number"),
            "customer": _customer_name(pkg),
            "shipped_date": _as_date(pkg.get("shipment_date")),
        }
        for pkg in packages
        if _lower(pkg.get("status")) == "delivered" and _as_date(pkg.get("shipment_date")) == today and not _package_sarisuki(pkg)
    ]


def _vendor_excluded(pr: dict) -> bool:
    vendor = _lower(pr.get("vendor_name"))
    number = _lower(pr.get("purchasereceive_number"))
    return "rare global" in vendor or "test" in vendor or "test" in number


def _purchase_receive_creator(detail: dict) -> str | None:
    for comment in detail.get("comments") or []:
        if _lower(comment.get("comment_type")) == "system" and re.search(r"purchase receive created", _text(comment.get("description") or comment.get("comment")), re.I):
            return _pick(comment, "commented_by", "created_by_name")
    return None


def _build_purchase_receives(purchase_receives: list[dict], errors: dict) -> dict:
    scanned = len(purchase_receives)
    base = [pr for pr in purchase_receives if not _vendor_excluded(pr)]
    not_billed = [pr for pr in base if _lower(pr.get("billed_status")) != "billed"]
    no_attachment_candidates = [pr for pr in base if pr.get("has_attachment") is False]
    details = _detail_map(
        [str(_pick(pr, "purchasereceive_id", "id") or "") for pr in no_attachment_candidates],
        fetch_purchase_receive_detail,
        "purchasereceive",
        5,
        errors,
    )
    rows_by_id: dict[str, dict] = {}
    for bucket, records in (("Not billed", not_billed), ("No attachment", no_attachment_candidates)):
        for pr in records:
            pr_id = str(_pick(pr, "purchasereceive_id", "id") or "")
            detail = details.get(pr_id, {})
            row = rows_by_id.setdefault(
                pr_id,
                {
                    "id": pr_id,
                    "pr_number": _pick(pr, "purchasereceive_number", "purchase_receive_number"),
                    "vendor": pr.get("vendor_name"),
                    "date": _as_date(_pick(pr, "date", "created_time")),
                    "created_by": _purchase_receive_creator(detail),
                    "buckets": [],
                },
            )
            row["buckets"].append(bucket)
    rows = sorted(rows_by_id.values(), key=lambda row: (row.get("date") or date.min), reverse=True)[:40]
    return {"not_billed": len(not_billed), "no_attachment": len(no_attachment_candidates), "scanned": scanned, "rows": rows}


@zoho_acquisition.operation("report-build", reuse_details=True)
def _build_report(section: str | None = None) -> dict:
    errors: dict[str, str] = {}
    today = _today()

    sections = {
        "sales-orders": {"sales_orders"},
        "packages": {"packages"},
        "inventory-adjustments": {"inventory_adjustments"},
        "transfer-orders": {"transfer_orders"},
        "purchase-receives": {"purchase_receives"},
        "invoices": {"invoices"},
    }
    if section is not None and section not in sections:
        raise HTTPException(400, f"Unknown report section: {section}")

    def fetch_if(resource: str, *args):
        # Keep compatibility with the existing call sites, which pass the
        # shared errors dict before the fetcher.
        fetcher = args[-1]
        return _safe_fetch(resource, errors, fetcher) if section is None or resource in sections[section] else []

    confirmed_sales_orders = fetch_if(
        "sales_orders",
        errors,
        lambda: _paginate(
            lambda page, per_page: fetch_sales_orders(page=page, per_page=per_page, filter_by="Status.Confirmed", sort_column="created_time"),
            "salesorders",
            20,
            stop_when=lambda rows: bool((oldest := _page_oldest(rows, "created_time")) and oldest < today - timedelta(days=30)),
        ),
    )
    time.sleep(0.4)
    not_shipped_packages = fetch_if(
        "packages",
        errors,
        lambda: _paginate(
            lambda page, per_page: fetch_packages(page=page, per_page=per_page, filter_by="Status.NotShipped", sort_column="date"),
            "packages",
            10,
            stop_when=lambda rows: bool((oldest := _page_oldest(rows, "date")) and oldest < today - timedelta(days=14)),
        ),
    )
    shipped_packages = fetch_if(
        "packages",
        errors,
        lambda: _paginate(lambda page, per_page: fetch_packages(page=page, per_page=per_page, filter_by="Status.Shipped", sort_column="shipment_date"), "packages", 3),
    )
    delivered_packages = fetch_if(
        "packages",
        errors,
        lambda: _paginate(
            lambda page, per_page: fetch_packages(page=page, per_page=per_page, filter_by="Status.Delivered", shipment_date_start=today, shipment_date_end=today),
            "packages",
            3,
        ),
    )
    time.sleep(0.4)
    inventory_adjustments = fetch_if(
        "inventory_adjustments",
        errors,
        lambda: _paginate(lambda page, per_page: fetch_inventory_adjustments(page=page, per_page=50, sort_column="date"), "inventory_adjustments", 24, per_page=50),
    )
    transfer_orders = fetch_if(
        "transfer_orders",
        errors,
        lambda: _paginate(lambda page, per_page: fetch_transfer_orders(page=page, per_page=per_page, sort_column="date"), "transferorders", 5),
    )
    purchase_receives = fetch_if(
        "purchase_receives",
        errors,
        lambda: _paginate(lambda page, per_page: fetch_purchase_receives(page=page, per_page=per_page, sort_column="created_time"), "purchasereceives", 3),
    )
    invoices = fetch_if(
        "invoices",
        errors,
        lambda: _paginate(lambda page, per_page: fetch_invoices(page=page, per_page=per_page, filter_by="Status.Draft", sort_column="date"), "invoices", 5),
    )

    due_not_packed = _build_due_not_packed(confirmed_sales_orders, today, errors)
    packed_not_shipped = _build_packed_not_shipped(not_shipped_packages, today, errors)
    shipped_not_delivered = _build_shipped_not_delivered(shipped_packages, errors)
    delivered_today = _build_delivered_today(delivered_packages, today)

    ia_pending = sorted(
        [
            {
                "id": str(_pick(ia, "inventory_adjustment_id", "id") or ""),
                "ia_type": _pick(ia, "reference_number", "reason") or "(no reference)",
                "qty": _pick(ia, "quantity_adjusted", "total_quantity_adjusted", "quantity"),
                "warehouse": _warehouse_tag(_warehouse_name(ia)),
                "date": _as_date(ia.get("date")),
            }
            for ia in {str(_pick(item, "inventory_adjustment_id", "id") or i): item for i, item in enumerate(inventory_adjustments)}.values()
            if _lower(ia.get("status")) == "pending_approval" and (d := _as_date(ia.get("date"))) and d >= today - timedelta(days=_IA_WINDOW_DAYS)
        ],
        key=lambda row: row.get("date") or date.min,
        reverse=True,
    )
    transfers = [
        {
            "id": str(_pick(to, "transfer_order_id", "id") or ""),
            "to_number": _pick(to, "transfer_order_number", "to_number"),
            "status": _lower(to.get("status")),
            "from": _pick(to, "from_warehouse_name", "source_warehouse_name") or (to.get("from_location") or {}).get("location_name"),
            "to": _pick(to, "to_warehouse_name", "destination_warehouse_name") or (to.get("to_location") or {}).get("location_name"),
            "qty": _pick(to, "quantity_transfer", "total_quantity_transfer", "quantity"),
            "date": _as_date(to.get("date")),
        }
        for to in transfer_orders
        if _lower(to.get("status")) in _TRANSFER_OPEN_STATUSES
    ]
    draft_invoices = [
        {
            "id": str(_pick(inv, "invoice_id", "id") or ""),
            "invoice_number": _pick(inv, "invoice_number", "number"),
            "customer": _customer_name(inv),
            "so_number": inv.get("reference_number"),
            "date": _as_date(inv.get("date")),
        }
        for inv in invoices
        if _lower(inv.get("status")) == "draft" and not _so_sarisuki(inv)
    ]
    purchase_receive_payload = _build_purchase_receives(purchase_receives, errors)

    due_groups = _group_rows(due_not_packed)
    packed_groups = _group_rows(packed_not_shipped)

    # 2026-09-24: removed the notify_packed_orders_batch call that used to run here - opening
    # a report must not write to Neon (message_log) or send notifications as a side effect.
    # If "order packed -> notify dispatcher" is still wanted, it should be rebuilt as its own
    # explicit trigger (most likely in n8n), not a side effect of fetching this report.

    def count_or_none(value: int, resource: str) -> int | None:
        return None if resource in errors else value

    return {
        "as_of": datetime.now(PHT).isoformat(),
        "today": today.isoformat(),
        "unavailable": sorted(key for key in errors.keys() if key != "detail_enrichment"),
        "errors": errors,
        "kpis": {
            "due_past_due_not_packed": count_or_none(sum(group["count"] for group in due_groups), "sales_orders"),
            "packed_not_shipped": count_or_none(sum(group["count"] for group in packed_groups), "packages"),
            "shipped_not_delivered": count_or_none(len(shipped_not_delivered), "packages"),
            "delivered_today": count_or_none(len(delivered_today), "packages"),
            "transfers_pending": count_or_none(len(transfers), "transfer_orders"),
            "ia_pending_approval": count_or_none(len(ia_pending), "inventory_adjustments"),
        },
        "order_fulfillment": {
            "due_past_due_not_packed": {"groups": due_groups},
            "packed_not_shipped": {"groups": packed_groups},
            "shipped_not_delivered": {"orders": shipped_not_delivered},
            "delivered_today": {"orders": delivered_today},
        },
        "transactions": {
            "inventory_adjustments_pending": ia_pending,
            "transfer_orders": transfers,
            "purchase_receives": purchase_receive_payload,
            "invoices_draft": {"count": count_or_none(len(draft_invoices), "invoices"), "orders": draft_invoices},
        },
    }


@router.get("/rgf-logistics")
def get_rgf_logistics_report(force: bool = False, section: str | None = None):
    epoch = zoho_acquisition.generation()
    identity = (os.environ.get("ZOHO_API_DOMAIN", "https://www.zohoapis.com"), os.environ.get("ZOHO_ORG_ID", ""), _today().isoformat())
    key = (identity, section, epoch)
    with _cache_lock:
        future = _report_inflight.get(key)
        if future is None and section is None and not force and _cache.get("identity") == identity and _cache["payload"] is not None and (time.time() - _cache["fetched_at"]) < _CACHE_TTL_SECONDS:
            zoho_acquisition.event("cache_hit", resource="report", section=section, force=force)
            return deepcopy(_cache["payload"])
        owner = future is None
        if owner:
            future = Future()
            _report_inflight[key] = future
    zoho_acquisition.event("cache_miss" if owner else "coalesced_waiter", resource="report", section=section, force=force, generation=epoch)
    if not owner:
        return deepcopy(future.result())
    try:
        payload = _build_report(section) if section is not None else _build_report()
        with zoho_acquisition.publication(epoch) as current:
            if current and section is None:
                with _cache_lock:
                    _cache.update(payload=deepcopy(payload), fetched_at=time.time(), identity=identity)
        future.set_result(deepcopy(payload))
        return payload
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _cache_lock:
            if _report_inflight.get(key) is future:
                del _report_inflight[key]


@router.get("/rgf-logistics/sales-order/{salesorder_id}")
def get_rgf_sales_order_detail(salesorder_id: str):
    try:
        return fetch_sales_order_detail(salesorder_id)
    except ZohoError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/rgf-logistics/purchase-receive/{purchasereceive_id}")
def get_rgf_purchase_receive_detail(purchasereceive_id: str):
    try:
        return fetch_purchase_receive_detail(purchasereceive_id)
    except ZohoError as exc:
        raise HTTPException(502, str(exc)) from exc
