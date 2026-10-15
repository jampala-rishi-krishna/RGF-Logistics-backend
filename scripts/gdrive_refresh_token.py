"""One-time: get the Google Drive refresh token of the photo-folder owner (martin@rareglobalfood.com). docs/GDRIVE_MEDIA.md step 3.

Run LOCALLY (your own machine, never on Render), signed in to Google as martin@ in the browser that opens:

    venv/Scripts/python.exe scripts/gdrive_refresh_token.py            (Windows)
    venv/bin/python scripts/gdrive_refresh_token.py                    (macOS / Linux)

The OAuth client is the existing Gmail one: GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET if set, else GMAIL_COMMS_CLIENT_ID /
GMAIL_COMMS_CLIENT_SECRET, read from the environment or backend/.env; when neither is found the script asks for them (the
secret is read without echo). Loopback flow (http://127.0.0.1:<port>/) with PKCE and a state check; scope
https://www.googleapis.com/auth/drive only. On Windows the refresh token is copied directly to the clipboard for pasting into
Render as GDRIVE_REFRESH_TOKEN. It is never printed or written to a file. It does not touch the Gmail token.

Options: --port N (default: a free port; a Web-type OAuth client needs that exact http://127.0.0.1:N/ registered as a redirect
URI, a Desktop-type client accepts any loopback port), --no-browser (print the URL instead of opening it).
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.server
import os
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ABOUT_URL = "https://www.googleapis.com/drive/v3/about"
SCOPE = "https://www.googleapis.com/auth/drive"
OWNER = "martin@rareglobalfood.com"


def _dotenv() -> dict[str, str]:
    path = Path(__file__).resolve().parent.parent / ".env"
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _setting(env: dict[str, str], *names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or env.get(name) or "").strip()
        if value:
            return value
    return ""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorize martin@'s Drive and copy the token privately to the Windows clipboard.")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("Run this local setup helper on Windows; secure clipboard delivery is required.", file=sys.stderr)
        return 2

    env = _dotenv()
    client_id = _setting(env, "GDRIVE_CLIENT_ID", "GMAIL_COMMS_CLIENT_ID") or input("OAuth client id: ").strip()
    client_secret = _setting(env, "GDRIVE_CLIENT_SECRET", "GMAIL_COMMS_CLIENT_SECRET") or getpass.getpass("OAuth client secret (hidden): ").strip()
    if not client_id or not client_secret:
        print("An OAuth client id and secret are required.", file=sys.stderr)
        return 2

    port = args.port or _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    url = AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE, "access_type": "offline",
        "prompt": "consent", "include_granted_scopes": "false", "login_hint": OWNER, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })

    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            query = parse_qs(urlparse(self.path).query)
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            if query.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "error" in query:
                result["error"] = query["error"][0]
            else:
                result["code"] = query["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")
            done.set()

        def log_message(self, *args):  # keep the code out of the terminal
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Sign in as {OWNER} and allow Google Drive access.")
    print(f"If no browser opens, visit:\n{url}\n")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        if not done.wait(timeout=600):
            print("Timed out after 10 minutes.", file=sys.stderr)
            return 1
    finally:
        server.shutdown()
    if "error" in result:
        print(f"Authorization failed: {result['error']}", file=sys.stderr)
        return 1

    response = httpx.post(TOKEN_URL, data={"code": result["code"], "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri,
                                           "grant_type": "authorization_code", "code_verifier": verifier}, timeout=30)
    if response.status_code != 200:
        print(f"Token exchange failed: HTTP {response.status_code} {response.json().get('error', '') if response.headers.get('content-type', '').startswith('application/json') else ''}", file=sys.stderr)
        return 1
    payload = response.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        print("Google returned no refresh token. Remove the app's access at https://myaccount.google.com/permissions and run again.", file=sys.stderr)
        return 1
    if SCOPE not in (payload.get("scope") or "").split():
        print(f"Warning: the granted scope is '{payload.get('scope')}', expected {SCOPE}.", file=sys.stderr)
    about = httpx.get(ABOUT_URL, params={"fields": "user(emailAddress)"}, headers={"Authorization": f"Bearer {payload['access_token']}"}, timeout=30)
    email = (about.json().get("user") or {}).get("emailAddress") if about.status_code == 200 else None
    print(f"Authorized Google account: {email or 'unknown'}")
    if not email or email.lower() != OWNER:
        print(f"Could not verify authorization as {OWNER}. Run again using that account; no token was copied.", file=sys.stderr)
        return 1
    subprocess.run(["clip.exe"], input=refresh_token, text=True, check=True)
    print("Refresh token copied privately to the clipboard. Paste into Render's GDRIVE_REFRESH_TOKEN, then clear the clipboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
