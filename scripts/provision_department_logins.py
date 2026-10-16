"""Create department logins through existing admin APIs; never write directly to Neon.

Dry run by default. Run with --apply --credentials-file /private/path/logins.json.
Loads admin credentials and PUBLIC_BACKEND_URL from backend/.env. Existing
accounts are checked for the correct active role and never reset or overwritten.
Generated passwords are saved before creation so an interrupted run can resume.
No login emails are sent. Never commit the credentials file.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

ACCOUNTS = (
    {"full_name": "Sales", "email": "sales@rgf.com", "role": "sales"},
    {"full_name": "Wet Market", "email": "wetmarket@rgf.com", "role": "wet_market"},
)


def request(base, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urlopen(req, timeout=90) as response:
            return json.load(response)
    except HTTPError as exc:
        # Do not echo response bodies: auth/user requests may contain secrets.
        raise SystemExit(f"{path}: HTTP {exc.code}; credentials were not printed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--base-url", help="Backend HTTPS origin (defaults to PUBLIC_BACKEND_URL)")
    parser.add_argument("--credentials-file", type=Path)
    args = parser.parse_args()
    if not args.apply:
        for account in ACCOUNTS:
            print(f"Would create {account['email']} with role {account['role']}")
        return
    if not args.credentials_file:
        parser.error("--apply requires --credentials-file outside source control")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    base = (args.base_url or os.environ.get("PUBLIC_BACKEND_URL", "")).rstrip("/")
    if not base.startswith("https://"):
        parser.error("Supply an HTTPS backend origin with --base-url or PUBLIC_BACKEND_URL")
    admin = request(base, "/auth/login", {
        "identifier": os.environ["SEED_ADMIN_EMAIL"],
        "password": os.environ["SEED_ADMIN_PASSWORD"],
    })
    if admin["user"]["role"] != "admin":
        raise SystemExit("Configured account is not an admin")
    token = admin["token"]
    existing = {u["email"].lower(): u for u in request(base, "/users", token=token)}
    credentials = json.loads(args.credentials_file.read_text()) if args.credentials_file.exists() else {}
    for account in ACCOUNTS:
        email = account["email"]
        user = existing.get(email)
        if user and (user["role"] != account["role"] or user["status"] != "active" or user.get("is_manager")):
            raise SystemExit(f"{email}: existing account has unexpected role/status; left unchanged")
        if user and email not in credentials:
            print(f"{email}: already exists; password left unchanged")
            continue
        if not user:
            credentials.setdefault(email, {"password": secrets.token_urlsafe(18), "role": account["role"]})
            args.credentials_file.parent.mkdir(parents=True, exist_ok=True)
            args.credentials_file.write_text(json.dumps(credentials, indent=2) + "\n")
            request(base, "/api/admin/users", {**account, "phone": None,
                    "password": credentials[email]["password"], "send_login_email": False}, token)
        session = request(base, "/auth/login", {"identifier": email, "password": credentials[email]["password"]})
        if session["user"]["role"] != account["role"] or session["user"].get("is_manager"):
            raise SystemExit(f"{email}: login role verification failed")
        print(f"{email}: active, login verified, role={account['role']}")
    print("Credentials saved locally; no passwords or tokens printed.")


if __name__ == "__main__":
    main()
