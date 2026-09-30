"""Repair only the local demo account password hashes."""

import os

from sqlalchemy import select

from auth.security import hash_password
from database import SessionLocal
from models.user import User


def main() -> None:
    password = os.environ.get("SEED_PASSWORD", "").strip()
    if not password:
        raise RuntimeError("SEED_PASSWORD is not configured")

    emails = {"dispatcher@rgf.test", "admin@rgf.test"}
    with SessionLocal() as db:
        users = db.execute(select(User).where(User.email.in_(emails))).scalars().all()
        for user in users:
            user.password_hash = hash_password(password)
            user.status = "active"
        db.commit()
        print(f"repaired_demo_accounts={len(users)}")


if __name__ == "__main__":
    main()
