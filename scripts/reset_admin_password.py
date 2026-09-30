"""Reset the configured test admin password without printing credentials."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from auth.security import hash_password  # noqa: E402
from database import SessionLocal  # noqa: E402
from models.user import User  # noqa: E402
from services import user_cache  # noqa: E402


def main() -> None:
    email = os.environ["SEED_ADMIN_EMAIL"].strip().lower()
    password = os.environ["SEED_ADMIN_PASSWORD"]
    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            raise SystemExit("Configured admin user was not found")
        user.password_hash = hash_password(password)
        user.status = "active"
        user.must_change_password = False
        db.commit()
        user_cache.invalidate(user.id)
    print("Admin password reset successfully")


if __name__ == "__main__":
    main()
