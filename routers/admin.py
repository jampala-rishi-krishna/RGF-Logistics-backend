from __future__ import annotations

import logging
import os
from email.utils import formataddr
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
from auth.security import hash_password, password_problem, unusable_password_hash
from database import get_db
from models.user import User
from services import app_settings, gmail_sender, memory_tables, staff_directory_cache, user_cache, user_invites, zoho_usage
from services.phone_numbers import ph_phone_digits
from services.staff_roles import staff_kind
from services.audit import list_audit_log as _list_audit_log

logger = logging.getLogger("admin")

router = APIRouter(tags=["admin"], dependencies=[Depends(require_role("admin"))])

# Same admin auth as the Users page. Lives under /api/admin like the other admin switches.
usage_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


@usage_router.get("/zoho-usage")
def get_zoho_usage():
    """Usage tile data. Does at most one Neon read per 5 minutes (refresh_if_stale), only while this is polled."""
    zoho_usage.refresh_if_stale()
    snap = zoho_usage.snapshot()
    return {
        "zoho_calls_today": snap["zoho_calls_today"],
        "process_started_at": snap["process_started_at"],
        "usage_restored_from_db": snap["usage_restored_from_db"],
        "zoho_usage": snap,
    }


@usage_router.get("/gps-cadence")
def get_gps_cadence():
    """How often each device really reports a new GPS fix (device event_ts), measured from the normal
    poller. In memory, no Cartrack call, no database. Resets on restart."""
    from services import gps_cadence

    return gps_cadence.snapshot()


class SettingBody(BaseModel):
    value: str | int | None = None


@usage_router.get("/settings")
def list_settings():
    """The whitelisted non-secret settings with their effective value and where it comes from. Memory only: no DB read."""
    return {"settings": app_settings.describe()}


@usage_router.put("/settings/{key}")
def save_setting(key: str, body: SettingBody, user: CurrentUser = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """The only write path of app_settings: one row, only when an admin presses Save."""
    from services.audit import write_audit_log

    if key not in app_settings.DEFINITIONS:
        raise HTTPException(404, "Unknown setting.")
    before = app_settings.describe_one(key)["value"]
    try:
        entry = app_settings.save(db, key, body.value, user.email)
    except app_settings.SettingError as exc:
        raise HTTPException(422, str(exc)) from exc
    write_audit_log(actor_id=user.email, action="setting_changed", target_entity="app_setting", target_id=key,
                    details={"from": before, "to": entry["value"]})
    return entry


@usage_router.delete("/settings/{key}")
def reset_setting(key: str, user: CurrentUser = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Drop an override so the environment value / built-in default applies again. One DELETE, only on an admin's click."""
    from services.audit import write_audit_log

    if key not in app_settings.DEFINITIONS:
        raise HTTPException(404, "Unknown setting.")
    before = app_settings.describe_one(key)["value"]
    entry = app_settings.clear(db, key)
    write_audit_log(actor_id=user.email, action="setting_reset", target_entity="app_setting", target_id=key,
                    details={"from": before, "to": entry["value"]})
    return entry


class TestAssignmentEmailBody(BaseModel):
    driver_email: str
    so_numbers: list[str] = []


@usage_router.post("/test-assignment-email")
def test_assignment_email(body: TestAssignmentEmailBody):
    """Send ONE assignment email, rendered by the real template and sent through the real Gmail path
    (same From, subject format, HTML + plain-text parts), to `driver_email` only. Nothing is assigned and
    Zoho is never called. The subject is marked [TEST]; the message is not labelled Logistics."""
    from routers import assignment
    from services import gmail_sender

    email = body.driver_email.strip()
    subject, html, text, source = assignment.render_test_assignment_email(email, body.so_numbers)
    try:
        result = gmail_sender.send_email(to=email, subject=subject, html=html, text=text, purpose="assignment-test", label_logistics=False)
    except gmail_sender.GmailSendError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    entry = (gmail_sender.recent(1) or [{}])[0]
    name, address = gmail_sender._identity(os.environ.get("GMAIL_FROM_NAME", "RareChain Logistics"), entry.get("sender"))
    return {"sent": True, "messageId": result.get("id"), "to": email, "subject": subject, "from": formataddr((name, address)) if name else address, "source": source}


VALID_INTEGRATION_STATES = {"not_connected", "sandbox", "live"}

# 2026-09-24 (Step 5/6): audit_log, integration_status, and roles all dropped from Neon
# (not in the final kept-table list) - in-memory/static only now.


@router.get("/audit-log")
def list_audit_log(targetEntity: str | None = None, actorId: str | None = None):
    return _list_audit_log(targetEntity, actorId)


@router.get("/integrations")
def list_integrations():
    return memory_tables.list_integration_status()


class IntegrationStateBody(BaseModel):
    state: str


@router.patch("/integrations/{provider}")
def upsert_integration_state(provider: str, body: IntegrationStateBody):
    if body.state not in VALID_INTEGRATION_STATES:
        raise HTTPException(400, f"state must be one of {', '.join(sorted(VALID_INTEGRATION_STATES))}")
    return memory_tables.upsert_integration_status(provider, body.state)


@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    users = db.execute(select(User).order_by(User.id)).scalars().all()
    states = user_invites.invite_states(db, [u.id for u in users])
    return [
        {
            "id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "role": u.role,
            "status": u.status,
            "profile_photo_url": u.profile_photo_url,
            "staff_id": u.staff_id,
            "must_change_password": u.must_change_password,
            "invite_state": states.get(u.id, "none"),
        }
        for u in users
    ]


@router.get("/roles")
def list_roles():
    return memory_tables.ROLES


# --- Driver / user logins: invite + reset links, enable / disable (all under /api/admin, admin only) ---


class InviteBody(BaseModel):
    purpose: str = "invite"  # "invite" (first login) | "reset" (forgotten password)


class UserStatusBody(BaseModel):
    status: str  # "active" | "disabled"


def _app_base_url(request: Request) -> str:
    """Where the SPA lives. APP_BASE_URL wins; otherwise the admin's own Origin, but only when it is one of the
    configured ALLOWED_ORIGINS (so a forged header can never point a link at another site)."""
    configured = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    allowed = [o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    origin = request.headers.get("origin", "").strip().rstrip("/")
    if origin and origin in allowed:
        return origin
    raise HTTPException(503, "APP_BASE_URL is not configured on the server, so an invite link cannot be built.")


def _user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found.")
    return user


def _purpose(body: InviteBody) -> str:
    if body.purpose not in user_invites.PURPOSES:
        raise HTTPException(400, "purpose must be 'invite' or 'reset'.")
    return body.purpose


def _new_link(db: Session, user: User, purpose: str, request: Request):
    base = _app_base_url(request)
    token, expires_at = user_invites.create_invite(db, user, purpose)
    db.commit()
    return f"{base}/set-password?token={token}", expires_at


@usage_router.post("/users/{user_id}/invite-link")
def create_invite_link(user_id: int, body: InviteBody, request: Request, db: Session = Depends(get_db), admin: CurrentUser = Depends(require_role("admin"))):
    """A fresh single-use link (valid 24 hours) for the admin to share manually, e.g. over WhatsApp/Viber. Earlier
    pending links for the user stop working. Nothing is emailed."""
    user = _user_or_404(db, user_id)
    purpose = _purpose(body)
    link, expires_at = _new_link(db, user, purpose, request)
    logger.info("[AUTH] %s link created for user_id=%s by admin_id=%s", purpose, user.id, admin.id)  # never log the link
    return {"link": link, "expires_at": expires_at.isoformat()}


def _invite_email(user: User, link: str, purpose: str) -> tuple[str, str, str]:
    name = escape((user.full_name or "").strip() or "there")
    if purpose == "reset":
        subject, lead = "Reset your IntelliFleet password", "A password reset was requested for your IntelliFleet account."
    else:
        subject, lead = "Your IntelliFleet driver login", "An IntelliFleet driver account has been created for you."
    button = "display:inline-block;background:#0b5fff;color:#ffffff;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:8px;font-size:15px;"
    html = (
        "<div style='font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#1f2933;max-width:520px;'>"
        f"<p>Hi {name},</p><p>{lead} Tap the button to choose your password. The link works once and expires in 24 hours.</p>"
        f"<p style='margin:22px 0;'><a href='{escape(link, quote=True)}' style='{button}'>Set my password</a></p>"
        "<p>Then log in with your email or your mobile number.</p>"
        "<p style='color:#52606d;font-size:13px;'>If you did not expect this email, ignore it - nothing changes until the link is used.</p></div>"
    )
    text = (
        f"Hi {user.full_name or 'there'},\n\n{lead} Open this link to choose your password (works once, expires in 24 hours):\n{link}\n\n"
        "Then log in with your email or your mobile number.\nIf you did not expect this email, ignore it."
    )
    return subject, html, text


@usage_router.post("/users/{user_id}/send-invite")
def send_invite(user_id: int, body: InviteBody, request: Request, db: Session = Depends(get_db), admin: CurrentUser = Depends(require_role("admin"))):
    """Creates a fresh link and emails it to the user's own address through the normal Gmail path (From: the
    configured sender, Martin Cuico)."""
    from services import gmail_sender

    user = _user_or_404(db, user_id)
    purpose = _purpose(body)
    if not (user.email or "").strip():
        raise HTTPException(422, "This user has no email address.")
    if not gmail_sender.configured():
        raise HTTPException(503, "Gmail is not configured on the server.")
    link, expires_at = _new_link(db, user, purpose, request)
    subject, html, text = _invite_email(user, link, purpose)
    try:
        gmail_sender.send_email(to=user.email, subject=subject, html=html, text=text, purpose="user-invite", label_logistics=False)
    except gmail_sender.GmailSendError as exc:
        raise HTTPException(exc.status_code, f"The invite email could not be sent: {exc}") from exc
    logger.info("[AUTH] %s email sent for user_id=%s by admin_id=%s", purpose, user.id, admin.id)
    return {"sent": True, "to": user.email, "expires_at": expires_at.isoformat()}


@usage_router.post("/users/{user_id}/status")
def set_user_status(user_id: int, body: UserStatusBody, db: Session = Depends(get_db), admin: CurrentUser = Depends(require_role("admin"))):
    if body.status not in {"active", "disabled"}:
        raise HTTPException(400, "status must be 'active' or 'disabled'.")
    user = _user_or_404(db, user_id)
    if user.id == admin.id and body.status != "active":
        raise HTTPException(409, "You cannot disable your own account.")
    user.status = body.status
    db.commit()
    user_cache.invalidate(user.id)  # takes effect on this instance's next request, not after the 10-minute TTL
    logger.info("[AUTH] user_id=%s set to %s by admin_id=%s", user.id, body.status, admin.id)
    return {"ok": True, "status": user.status}


# --- Create a login (admin only). The user gets an unusable password; share an invite link afterwards. ---

CREATABLE_ROLES = ("logistics_officer", "driver", "helper", "dispatcher", "planner", "warehouse")  # never admin


class CreateUserBody(BaseModel):
    full_name: str
    email: str
    phone: str
    role: str
    staff_id: int | None = None
    password: str | None = None
    send_login_email: bool = False


def _user_row(u: User) -> dict:
    return {"id": u.id, "full_name": u.full_name, "email": u.email, "phone": u.phone, "role": u.role, "status": u.status,
            "profile_photo_url": u.profile_photo_url, "staff_id": u.staff_id, "must_change_password": u.must_change_password, "invite_state": "none"}


def _display_phone(phone) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("63") and len(digits) == 12:
        return "0" + digits[2:]
    return digits


def _login_details_email(user: User, password: str, staff: dict | None) -> tuple[str, str, str]:
    name = escape((user.full_name or "").strip() or "there")
    email = escape(user.email or "")
    phone = escape(_display_phone(user.phone))
    role = escape(user.role or "")
    staff_id = escape(str(user.staff_id or ""))
    title = escape(str((staff or {}).get("title") or ""))
    warehouse = escape(str((staff or {}).get("warehouse") or ""))
    rows = [
        ("Login email", email),
        ("Login phone", phone),
        ("Password", escape(password)),
        ("Role", role),
    ]
    if staff_id:
        rows.append(("Staff ID", staff_id))
    if title:
        rows.append(("Title", title))
    if warehouse:
        rows.append(("Warehouse", warehouse))
    row_html = "".join(
        "<tr>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;color:#6b7280;font-size:13px;'>{label}</td>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;font-weight:700;color:#111827;'>{value}</td>"
        "</tr>"
        for label, value in rows
    )
    subject = "Your IntelliFleet login details"
    html = (
        "<div style='font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#1f2933;max-width:560px;'>"
        "<div style='border-bottom:3px solid #111827;padding-bottom:12px;margin-bottom:20px;'>"
        "<div style='font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#6b7280;'>Rare Global Food Trading Corp.</div>"
        "<h1 style='font-size:26px;line-height:1.1;margin:8px 0 0;color:#111827;'>IntelliFleet login</h1>"
        "</div>"
        f"<p>Hi {name},</p>"
        "<p>Your IntelliFleet account is ready. Use the details below to sign in on the driver portal.</p>"
        "<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border:1px solid #e5e7eb;border-collapse:collapse;margin:18px 0;'>"
        f"{row_html}</table>"
        "<p style='color:#6b7280;font-size:13px;'>Keep this email private. If you did not expect this login, contact dispatch before signing in.</p>"
        "</div>"
    )
    text = "\n".join(
        [
            f"Hi {user.full_name or 'there'},",
            "",
            "Your IntelliFleet account is ready. Use these details to sign in:",
            f"Login email: {user.email}",
            f"Login phone: {_display_phone(user.phone)}",
            f"Password: {password}",
            f"Role: {user.role}",
            *( [f"Staff ID: {user.staff_id}"] if user.staff_id else [] ),
            *( [f"Title: {(staff or {}).get('title')}"] if title else [] ),
            *( [f"Warehouse: {(staff or {}).get('warehouse')}"] if warehouse else [] ),
            "",
            "Keep this email private. If you did not expect this login, contact dispatch before signing in.",
        ]
    )
    return subject, html, text


@usage_router.post("/users", status_code=201)
def create_user(body: CreateUserBody, db: Session = Depends(get_db), admin: CurrentUser = Depends(require_role("admin"))):
    role = (body.role or "").strip().lower()
    if role not in CREATABLE_ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(CREATABLE_ROLES)}.")
    name = (body.full_name or "").strip()
    email = (body.email or "").strip().lower()
    if not name:
        raise HTTPException(400, "full_name is required.")
    if "@" not in email or " " in email or len(email) < 5:
        raise HTTPException(400, "A valid email address is required.")
    initial_password = body.password or ""
    if body.send_login_email and not initial_password:
        raise HTTPException(400, "Enter an initial password before emailing login details.")
    if initial_password:
        problem = password_problem(initial_password)
        if problem:
            raise HTTPException(400, problem)
    phone = ph_phone_digits(body.phone)
    if phone is None:
        raise HTTPException(400, "phone must be a Philippine mobile number (09XXXXXXXXX or +639XXXXXXXXX).")
    staff_id = body.staff_id
    staff = None
    if role in ("driver", "helper"):
        if staff_id is None:
            raise HTTPException(400, f"A {role} login must be linked to a staff record (staff_id).")
        staff = staff_directory_cache.get_by_id(staff_id)
        if not staff:
            raise HTTPException(400, f"Staff record {staff_id} was not found in the staff directory.")
        if staff_kind(staff) != role:
            raise HTTPException(400, f"Staff record {staff_id} is a {staff_kind(staff)}, so its login role must be {staff_kind(staff)}.")
        if db.execute(select(User.id).where(User.staff_id == staff_id)).first() is not None:
            raise HTTPException(409, f"Staff record {staff_id} already has a login.")
    else:
        staff_id = None  # only drivers / helpers are linked to the staff directory
    if db.execute(select(User.id).where(User.email == email)).first() is not None:
        raise HTTPException(409, "A user with this email already exists.")
    if db.execute(select(User.id).where(User.phone == phone)).first() is not None:
        raise HTTPException(409, "A user with this phone number already exists.")
    if body.send_login_email:
        if not gmail_sender.configured():
            raise HTTPException(503, "Gmail is not configured on the server.")

    user = User(
        full_name=name,
        email=email,
        phone=phone,
        role=role,
        status="active",
        staff_id=staff_id,
        password_hash=hash_password(initial_password) if initial_password else unusable_password_hash(),
        must_change_password=False,
    )
    db.add(user)
    try:
        db.flush()
        if body.send_login_email and initial_password:
            subject, html, text = _login_details_email(user, initial_password, staff)
            gmail_sender.send_email(to=user.email, subject=subject, html=html, text=text, purpose="user-login-details", label_logistics=False)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A user with this email, phone or staff record already exists.") from None
    except gmail_sender.GmailSendError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, f"The login details email could not be sent: {exc}") from exc
    logger.info("[AUTH] user_id=%s created with role=%s by admin_id=%s", user.id, role, admin.id)
    return _user_row(user)
