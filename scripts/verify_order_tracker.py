"""Read-only deployed Tracker comparison. Never calls Ask or changes business records.

Uses existing SEED_ADMIN_EMAIL/PASSWORD from the local backend .env, in memory
only. One login attempt; no credential reset, no credential/token output.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="https://rgf-logistics-backend.onrender.com")
    args = parser.parse_args()
    if urlparse(args.api).hostname != "rgf-logistics-backend.onrender.com" or urlparse(args.api).scheme != "https":
        raise SystemExit("Refusing an unrecognized production API origin.")
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    report = {"read_only": True, "live_ai_called": False, "orders": []}
    with httpx.Client(base_url=args.api, timeout=40, follow_redirects=False) as client:
        health = client.get("/health"); health.raise_for_status()
        payload = health.json()
        report["health"] = {k: payload.get(k) for k in ("status", "rss_mb", "instance_id", "workers")}
        report["tracker_api_present"] = "/api/order-tracker/orders" in client.get("/openapi.json").json().get("paths", {})
        if not report["tracker_api_present"]:
            report["blocked"] = "Tracker API deployment has not reached this instance."
        elif not env.get("SEED_ADMIN_EMAIL") or not env.get("SEED_ADMIN_PASSWORD"):
            report["blocked"] = "Local administrator credentials are not configured; no login was attempted."
        else:
            login = client.post("/auth/login", json={"identifier": env["SEED_ADMIN_EMAIL"], "password": env["SEED_ADMIN_PASSWORD"]})
            if login.status_code != 200 or login.json().get("user", {}).get("role") != "admin":
                report["blocked"] = "One administrator login attempt did not provide an admin session; no records were changed."
            else:
                client.headers["Authorization"] = "Bearer " + login.json()["token"]
                day = datetime.now(ZoneInfo("Asia/Manila")).date().isoformat()
                report["date"] = day
                overview = client.get("/api/driver-overview/trips", params={"date": day}); overview.raise_for_status()
                trips = overview.json().get("trips", [])
                listing = client.get("/api/order-tracker/orders", params={"date_from": day, "date_to": day}); listing.raise_for_status()
                rows = listing.json().get("orders", [])
                # Prefer assigned SOs, whose trip facts can be compared directly.
                rows.sort(key=lambda r: not bool(r.get("truck")))
                for row in [r for r in rows if r.get("order_type") in {"SO", "WM"}][:3]:
                    number = row["number"]
                    matches = client.get("/api/driver-overview/trips", params={"date": day, "q": number}); matches.raise_for_status()
                    matched = matches.json().get("trips", [])
                    timeline = None
                    if matched:
                        response = client.get("/api/driver-overview/trips/" + quote(matched[0]["batch_id"], safe="")); response.raise_for_status()
                        timeline = response.json()
                    response = client.get("/api/order-tracker/orders/" + quote(number, safe=""), params={"date_from": day, "date_to": day}); response.raise_for_status()
                    card = response.json()
                    source = next((o for o in (timeline or {}).get("orders", []) if o.get("so_number") == number), {})
                    source_stop = source.get("stop") or {}
                    checks = []
                    pairs = {"departure": (card["sections"]["times"]["value"].get("left_warehouse"), (timeline or {}).get("trip", {}).get("started_at")),
                             "arrival": (card["sections"]["times"]["value"].get("arrived"), (source_stop.get("checkin") or {}).get("at")),
                             "checkout": (card["sections"]["times"]["value"].get("checkout_at"), (source_stop.get("checkout") or {}).get("at")),
                             "outcome": (card["sections"]["delivery_result"]["value"].get("outcome"), (source_stop.get("checkout") or {}).get("outcome"))}
                    for key, (value, expected) in pairs.items():
                        checks.append({"field": key, "result": "match" if value == expected and expected is not None else "not recorded in both" if expected is None and value is None else "mismatch"})
                    report["orders"].append({"order": number, "status": card["status"], "overview_available": bool(source),
                        "comparisons": checks, "sections": {k: {"state": v["state"], "as_of": v["as_of"]} for k, v in card["sections"].items()}})
                if len(report["orders"]) < 3:
                    report["limitation"] = "Fewer than three accessible cached current-day SOs were available. No provider refresh was triggered."
                report["overview_trip_count"] = len(trips)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        # Exception strings may contain request details. Report only type/status.
        print(json.dumps({"error_type": type(exc).__name__, "status": getattr(getattr(exc, "response", None), "status_code", None)}))
        raise SystemExit(1)
