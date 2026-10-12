"""Create driver logins (role=driver) linked to staff-directory records. DRY RUN unless --apply is given.

    python scripts/seed_drivers.py                      # show what would be created for staff ids 9-16
    python scripts/seed_drivers.py --apply              # create them
    python scripts/seed_drivers.py --staff-id 22 --email me+drivertest@example.com --phone 09171234567 --apply
                                                        # one TEST driver for an end-to-end check (never touches 9-16)

Name, email and phone are read from the staff directory (backend/.cache snapshot; add --refresh to re-read the n8n
table first). Each account gets an unusable random password hash - nobody can log in until an invite / reset link is
used (Admin > Users). No password, token or link is ever printed. Existing logins are left untouched.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

DEFAULT_STAFF_IDS = list(range(9, 17))


def parse_ids(text: str) -> list[int]:
    ids: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            low, high = part.split("-", 1)
            ids.extend(range(int(low), int(high) + 1))
        elif part:
            ids.append(int(part))
    return ids


def plan_accounts(staff_ids, staff_lookup, *, email=None, phone=None, name=None):
    """[(staff_id, full_name, email, phone_digits, problem)] - pure, no database."""
    from services.phone_numbers import ph_phone_digits

    plans = []
    for staff_id in staff_ids:
        staff = staff_lookup(staff_id)
        if not staff and not (name and email and phone):
            plans.append((staff_id, None, None, None, "staff record not found in the directory snapshot (use --refresh, or pass --name/--email/--phone)"))
            continue
        staff = staff or {}
        full_name = (name or staff.get("name") or "").strip()
        login_email = (email or staff.get("email") or "").strip().lower()
        digits = ph_phone_digits(phone or staff.get("phone"))
        problem = None
        if not full_name:
            problem = "no name"
        elif not login_email or "@" not in login_email:
            problem = "no valid email"
        elif digits is None:
            problem = "no valid PH mobile number"
        plans.append((staff_id, full_name, login_email, digits, problem))
    return plans


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--staff-ids", default="9-16", help="e.g. 9-16 or 9,10,12 (default 9-16)")
    parser.add_argument("--staff-id", type=int, help="a single staff id (for a test driver)")
    parser.add_argument("--name"), parser.add_argument("--email"), parser.add_argument("--phone")
    parser.add_argument("--refresh", action="store_true", help="re-read the staff directory from n8n first")
    parser.add_argument("--apply", action="store_true", help="actually write; without it nothing is changed")
    args = parser.parse_args()

    from sqlalchemy import select

    from auth.security import unusable_password_hash
    from database import SessionLocal
    from models.user import User
    from services import staff_directory_cache

    if args.refresh:
        staff_directory_cache.refresh()
    ids = [args.staff_id] if args.staff_id is not None else parse_ids(args.staff_ids)
    if args.staff_id is not None and args.staff_id in DEFAULT_STAFF_IDS and (args.email or args.phone or args.name):
        print("Refusing to override the details of staff ids 9-16. Use a different (test) staff record.")
        return 2
    overrides = {"email": args.email, "phone": args.phone, "name": args.name} if args.staff_id is not None else {}
    plans = plan_accounts(ids, staff_directory_cache.get_by_id, **overrides)

    created = skipped = failed = 0
    with SessionLocal() as db:
        for staff_id, full_name, email, digits, problem in plans:
            if problem:
                print(f"staff {staff_id}: SKIPPED - {problem}")
                failed += 1
                continue
            clash = db.execute(select(User).where((User.staff_id == staff_id) | (User.email == email) | (User.phone == digits))).scalars().first()
            if clash is not None:
                same = clash.staff_id == staff_id
                print(f"staff {staff_id} {full_name}: {'already has a login (user id %s)' % clash.id if same else 'email/phone already used by user id %s' % clash.id} - left unchanged")
                skipped += 1 if same else 0
                failed += 0 if same else 1
                continue
            print(f"staff {staff_id} {full_name}: {'creating' if args.apply else 'would create'} driver login")
            if args.apply:
                db.add(User(full_name=full_name, email=email, phone=digits, role="driver", status="active", staff_id=staff_id, password_hash=unusable_password_hash(), must_change_password=False))
            created += 1
        if args.apply:
            db.commit()
    print(f"\n{'Created' if args.apply else 'Would create'} {created}, already existed {skipped}, problems {failed}." + ("" if args.apply else "  (dry run - add --apply to write)"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
