"""Object storage (Cloudflare R2, S3-compatible) for trip photos / signatures. Never Neon. Contract: docs/TRIP_WORKFLOW.md section 9.

AWS Signature V4 implemented with hmac / hashlib and sent with httpx (no boto3, no new dependency). Path-style addressing:
https://<account>.r2.cloudflarestorage.com/<bucket>/<key>, region "auto", service "s3". Secrets are read from the
environment on every call and are never logged or returned.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

REQUIRED_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
REGION = "auto"
SERVICE = "s3"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


class StorageError(Exception):
    pass


def is_enabled() -> bool:
    """True only when every R2 variable is set."""
    return all(os.environ.get(name, "").strip() for name in REQUIRED_ENV)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _endpoint() -> str:
    return (_env("R2_ENDPOINT") or f"https://{_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com").rstrip("/")


# ---- SigV4 primitives (pure; exercised by the AWS published test vectors) ----------------------------------------


def uri_encode(value: str, *, keep_slash: bool = False) -> str:
    return quote(value, safe="-_.~" + ("/" if keep_slash else ""))


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return key


def canonical_request(method: str, path: str, query: dict[str, str], headers: dict[str, str], payload_hash: str) -> tuple[str, str]:
    """(canonical request text, signed header names). `headers` are the headers to sign; names are lower-cased here."""
    lowered = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    names = sorted(lowered)
    canonical_query = "&".join(f"{uri_encode(k)}={uri_encode(v)}" for k, v in sorted(query.items()))
    canonical_headers = "".join(f"{name}:{lowered[name]}\n" for name in names)
    signed = ";".join(names)
    text = "\n".join([method.upper(), uri_encode(path, keep_slash=True), canonical_query, canonical_headers, signed, payload_hash])
    return text, signed


def signature_for(canonical: str, *, secret: str, amz_date: str, region: str, service: str) -> str:
    date_stamp = amz_date[:8]
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    return hmac.new(signing_key(secret, date_stamp, region, service), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _object_url_parts(key: str) -> tuple[str, str, str]:
    endpoint = _endpoint()
    host = endpoint.split("://", 1)[-1]
    return endpoint, host, f"/{_env('R2_BUCKET')}/{key.lstrip('/')}"


# ---- operations ---------------------------------------------------------------------------------------------------


def put_object(key: str, data: bytes, content_type: str, *, now: datetime | None = None) -> None:
    """Upload bytes to `key`. Raises StorageError when storage is not configured or the request fails."""
    if not is_enabled():
        raise StorageError("object storage is not configured")
    endpoint, host, path = _object_url_parts(key)
    amz_date = _stamp(now)
    payload_hash = hashlib.sha256(data).hexdigest()
    headers = {"host": host, "content-type": content_type, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    canonical, signed = canonical_request("PUT", path, {}, headers, payload_hash)
    signature = signature_for(canonical, secret=_env("R2_SECRET_ACCESS_KEY"), amz_date=amz_date, region=REGION, service=SERVICE)
    scope = f"{amz_date[:8]}/{REGION}/{SERVICE}/aws4_request"
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={_env('R2_ACCESS_KEY_ID')}/{scope}, SignedHeaders={signed}, Signature={signature}"
    send_headers = {name: value for name, value in headers.items() if name != "host"}  # httpx sets Host itself
    try:
        response = httpx.put(endpoint + quote(path, safe="/-_.~"), content=data, headers=send_headers, timeout=30)
    except httpx.HTTPError as exc:
        raise StorageError(f"storage upload failed: {type(exc).__name__}") from exc
    if response.status_code >= 300:
        raise StorageError(f"storage upload failed (HTTP {response.status_code})")


def presign_get(key: str, expires: int = 600, *, now: datetime | None = None) -> str:
    """A time-limited GET URL for `key` (query-string SigV4). Raises StorageError when storage is not configured."""
    if not is_enabled():
        raise StorageError("object storage is not configured")
    endpoint, host, path = _object_url_parts(key)
    amz_date = _stamp(now)
    scope = f"{amz_date[:8]}/{REGION}/{SERVICE}/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{_env('R2_ACCESS_KEY_ID')}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
    }
    canonical, _ = canonical_request("GET", path, query, {"host": host}, UNSIGNED_PAYLOAD)
    signature = signature_for(canonical, secret=_env("R2_SECRET_ACCESS_KEY"), amz_date=amz_date, region=REGION, service=SERVICE)
    query_string = "&".join(f"{uri_encode(k)}={uri_encode(v)}" for k, v in sorted(query.items()))
    return f"{endpoint}{quote(path, safe='/-_.~')}?{query_string}&X-Amz-Signature={signature}"
