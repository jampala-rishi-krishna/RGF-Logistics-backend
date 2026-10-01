"""Phase 3: compute Zoho API usage metrics from production logs. Read-only; makes no Zoho calls.

    python scripts/analyze_zoho_logs.py render.log [more.log ...] [--json] [--since 2026-10-01] [--until 2026-10-08]

Input is the app's normal log output (`%(asctime)s %(name)s %(levelname)s %(message)s`); only
`[ZOHO_ACQUIRE] event=...` lines are used. Every number is counted from log lines, never inferred.
Feature is derived from the request route (the route is logged without query string).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

LINE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]?\d*\s.*?\[ZOHO_ACQUIRE\] event=(?P<event>\w+) (?P<rest>.*)$")
ID = re.compile(r"/[0-9]{6,}|/[0-9a-f]{12,}", re.I)

FEATURES = [  # first match wins; matched against the logged route
    ("Fleet", ("/vehicles", "/fleet")),
    ("Dispatch", ("/dispatch", "/pipeline")),
    ("Reports", ("/reports",)),
    ("Assignment", ("/assignment",)),
    ("Inventory drawer/ack", ("/load-planning/inventory/sales-orders/",)),
    ("Load Planning", ("/load-planning",)),
]


def feature_of(route: str, source: str) -> str:
    if route == "background":
        return {"fleet-refresh": "Fleet (scheduler)"}.get(source, "Background")
    for name, needles in FEATURES:
        if any(n in route for n in needles):
            return name
    return "Other"


def endpoint_class(endpoint: str) -> str:
    parts = endpoint.strip("/").split("/")
    resource = parts[0] if parts else "?"
    kind = "detail" if len(parts) == 2 else "list" if len(parts) == 1 else "sub/" + "/".join(parts[2:])
    return f"{resource}:{kind}"


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def stats(values: list[float]) -> dict:
    return {"avg": round(sum(values) / len(values), 1) if values else 0.0, "p50": pct(values, .5), "p95": pct(values, .95), "max": max(values, default=0.0)}


def parse(lines, since: str | None = None, until: str | None = None):
    for raw in lines:
        m = LINE.match(raw.rstrip("\n"))
        if not m or (since and m["ts"][:10] < since) or (until and m["ts"][:10] > until):
            continue
        fields = dict(part.split("=", 1) for part in m["rest"].split() if "=" in part)
        yield datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S"), m["event"], fields


def analyze(lines, since=None, until=None) -> dict:
    total = Counter()
    per_day = defaultdict(Counter)
    per_hour = defaultdict(Counter)
    per_minute = Counter()
    per_feature = defaultdict(Counter)
    per_endpoint = defaultdict(Counter)
    retry_by_cause = Counter()
    outcomes = Counter()
    rate_wait, conc_wait = [], []
    durations = defaultdict(list)
    first_attempts = Counter()  # (request_id, endpoint) -> initial attempts, to find duplicate acquisition
    logical_meta: dict[str, tuple[str, str]] = {}
    report_rows = []

    for ts, event, f in parse(lines, since, until):
        feature = feature_of(f.get("route", ""), f.get("source", ""))
        day, hour = ts.strftime("%Y-%m-%d"), ts.strftime("%H")
        if event == "logical_request":
            total["logical"] += 1
            per_day[day]["logical"] += 1
            per_feature[feature]["logical"] += 1
            if "endpoint" in f:
                per_endpoint[endpoint_class(f["endpoint"])]["logical"] += 1
            if f.get("method") != "GET" and f.get("method"):
                total["logical_writes"] += 1
        elif event == "cache_hit":
            total["cache_hits"] += 1
            if "endpoint" in f:
                per_endpoint[endpoint_class(f["endpoint"])]["cache_hits"] += 1
            per_feature[feature]["cache_hits"] += 1
        elif event == "cache_miss":
            total["cache_misses"] += 1
        elif event == "coalesced_waiter":
            total["coalesced"] += 1
            if "endpoint" in f:
                per_endpoint[endpoint_class(f["endpoint"])]["coalesced"] += 1
            per_day[day]["coalesced"] += 1
            per_feature[feature]["coalesced"] += 1
        elif event == "http_attempt":
            ep = endpoint_class(f.get("endpoint", "?"))
            total["http"] += 1
            total["http_" + f.get("method", "GET").lower()] += 1
            per_day[day]["http"] += 1
            per_hour[hour]["http"] += 1
            per_minute[ts.strftime("%Y-%m-%d %H:%M")] += 1
            per_feature[feature]["http"] += 1
            per_endpoint[ep]["http"] += 1
            if f.get("retry") == "0":
                first_attempts[(f.get("request_id"), f.get("source"), f.get("endpoint"))] += 1 if f.get("request_id") != "background" else 0
            else:
                total["retry_attempts_seen"] += 1
            rate_wait.append(float(f.get("rate_wait_ms", 0)))
            conc_wait.append(float(f.get("concurrency_wait_ms", 0)))
        elif event == "http_outcome":
            outcomes[f.get("category", "?")] += 1
            if f.get("status") == "429":
                total["429"] += 1
                per_day[day]["429"] += 1
                per_hour[hour]["429"] += 1
                per_feature[feature]["429"] += 1
                per_endpoint[endpoint_class(f.get("endpoint", "?"))]["429"] += 1
            if f.get("ms", "").isdigit():
                durations[endpoint_class(f.get("endpoint", "?"))].append(float(f["ms"]))
        elif event == "http_retry":
            total["retries"] += 1
            per_day[day]["retries"] += 1
            per_feature[feature]["retries"] += 1
            per_endpoint[endpoint_class(f.get("endpoint", "?"))]["retries"] += 1
            retry_by_cause[f.get("status", "?")] += 1
            if f.get("retry_after") == "missing":
                total["retry_after_missing"] += 1
        elif event == "http_failure":
            total["terminal_failures"] += 1
        elif event == "report_candidates":
            report_rows.append(f)

    http = total["http"]
    logical = total["logical"]
    duplicates = {f"{src}:{ep}": n for (rid, src, ep), n in first_attempts.items() if n > 1}
    return {
        "executive": {
            "logical_requests": logical, "http_attempts": http, "get_attempts": total["http_get"],
            "write_attempts": http - total["http_get"], "retry_attempts": total["retries"],
            "retry_rate_pct": round(100 * total["retries"] / http, 2) if http else 0.0,
            "http_429": total["429"], "cache_hits": total["cache_hits"], "cache_misses": total["cache_misses"],
            "coalesced_waiters": total["coalesced"],
            "coalescing_ratio_pct": round(100 * total["coalesced"] / logical, 2) if logical else 0.0,
            "http_calls_avoided_by_coalescing": total["coalesced"],
            "days_observed": len(per_day), "avg_http_per_day": round(http / len(per_day), 1) if per_day else 0.0,
            "peak_http_per_minute": max(per_minute.values(), default=0),
            "minutes_at_or_above_80": sum(1 for v in per_minute.values() if v >= 80),
            "retry_after_missing": total["retry_after_missing"], "terminal_failures": total["terminal_failures"],
        },
        "per_day": {d: dict(c) for d, c in sorted(per_day.items())},
        "per_hour_http": {h: c["http"] for h, c in sorted(per_hour.items())},
        "per_hour_429": {h: c["429"] for h, c in sorted(per_hour.items()) if c["429"]},
        "per_feature": {k: dict(v) for k, v in sorted(per_feature.items(), key=lambda kv: -kv[1]["http"])},
        "per_endpoint": {k: {**v, "pct_of_http": round(100 * v["http"] / http, 1) if http else 0.0} for k, v in sorted(per_endpoint.items(), key=lambda kv: -kv[1]["http"])},
        "retries_by_cause": dict(retry_by_cause),
        "attempt_outcomes": dict(outcomes),
        "rate_wait_ms": stats(rate_wait), "concurrency_wait_ms": stats(conc_wait),
        "http_ms_by_endpoint": {k: stats(v) for k, v in durations.items()},
        "duplicate_initial_fetches_within_one_request": {"count": len(duplicates), "examples": dict(list(duplicates.items())[:10])},
        "report_candidate_events": len(report_rows),
    }


def render(result: dict) -> str:
    out = []
    for section, data in result.items():
        out.append(f"== {section}")
        if isinstance(data, dict):
            for k, v in data.items():
                out.append(f"  {k}: {v}")
        else:
            out.append(f"  {data}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    def lines():
        for path in args.logs:
            with open(path, encoding="utf-8", errors="replace") as handle:
                yield from handle
    result = analyze(lines(), args.since, args.until)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
