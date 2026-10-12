"""R2 storage: SigV4 against published AWS vectors, upload endpoint (idempotent, crew-only, magic bytes), signed redirect. No network."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from models.trip import TripPhoto
from routers import media
from services import media_storage
from tests import trip_test_support as t

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
R2 = {"R2_ACCOUNT_ID": "acct123", "R2_ACCESS_KEY_ID": "AKIATESTKEY", "R2_SECRET_ACCESS_KEY": "test-secret-value-not-real", "R2_BUCKET": "trip-photos"}


# ---- SigV4 against published AWS test vectors ---------------------------------------------------------------------


def test_sigv4_matches_the_aws_test_suite_get_vanilla_vector():
    """AWS Signature V4 test suite, 'get-vanilla': AKIDEXAMPLE, 20150830T123600Z, us-east-1/service."""
    canonical, signed = media_storage.canonical_request("GET", "/", {}, {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"}, hashlib.sha256(b"").hexdigest())
    assert canonical == "GET\n/\n\nhost:example.amazonaws.com\nx-amz-date:20150830T123600Z\n\nhost;x-amz-date\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert signed == "host;x-amz-date"
    assert hashlib.sha256(canonical.encode()).hexdigest() == "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63"
    signature = media_storage.signature_for(canonical, secret="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", amz_date="20150830T123600Z", region="us-east-1", service="service")
    assert signature == "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"


def test_sigv4_matches_the_aws_s3_presigned_url_example():
    """The worked example in the S3 docs (query-string auth, GET /test.txt, 86400 s)."""
    query = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": "AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request", "X-Amz-Date": "20130524T000000Z", "X-Amz-Expires": "86400", "X-Amz-SignedHeaders": "host"}
    canonical, _ = media_storage.canonical_request("GET", "/test.txt", query, {"host": "examplebucket.s3.amazonaws.com"}, media_storage.UNSIGNED_PAYLOAD)
    signature = media_storage.signature_for(canonical, secret="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", amz_date="20130524T000000Z", region="us-east-1", service="s3")
    assert signature == "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"


def test_is_enabled_needs_every_variable(monkeypatch):
    for name in R2:
        monkeypatch.delenv(name, raising=False)
    assert media_storage.is_enabled() is False
    for name, value in R2.items():
        monkeypatch.setenv(name, value)
        assert media_storage.is_enabled() is (name == "R2_BUCKET")
    monkeypatch.setenv("R2_BUCKET", "  ")
    assert media_storage.is_enabled() is False


def test_presign_get_is_signed_expiring_and_leaks_no_secret(monkeypatch):
    for name, value in R2.items():
        monkeypatch.setenv(name, value)
    now = datetime(2026, 10, 12, 1, 2, 3, tzinfo=timezone.utc)
    url = media_storage.presign_get("trips/b1/abc.jpg", expires=600, now=now)
    assert url.startswith("https://acct123.r2.cloudflarestorage.com/trip-photos/trips/b1/abc.jpg?")
    for part in ("X-Amz-Algorithm=AWS4-HMAC-SHA256", "X-Amz-Date=20261012T010203Z", "X-Amz-Expires=600", "X-Amz-SignedHeaders=host", "X-Amz-Credential=AKIATESTKEY%2F20261012%2Fauto%2Fs3%2Faws4_request"):
        assert part in url
    assert "X-Amz-Signature=" in url and R2["R2_SECRET_ACCESS_KEY"] not in url
    assert media_storage.presign_get("trips/b1/abc.jpg", 600, now=now) == url  # deterministic
    monkeypatch.setenv("R2_ENDPOINT", "https://custom.example.test/")
    assert media_storage.presign_get("k.jpg", now=now).startswith("https://custom.example.test/trip-photos/k.jpg?")
    monkeypatch.delenv("R2_BUCKET")
    with pytest.raises(media_storage.StorageError):
        media_storage.presign_get("k.jpg")


def test_put_object_signs_the_request_and_reports_failures(monkeypatch):
    for name, value in R2.items():
        monkeypatch.setenv(name, value)
    calls = []
    monkeypatch.setattr(httpx, "put", lambda url, **kw: calls.append((url, kw)) or SimpleNamespace(status_code=200))
    media_storage.put_object("trips/b1/x.png", PNG, "image/png", now=datetime(2026, 10, 12, 1, 2, 3, tzinfo=timezone.utc))
    url, kw = calls[0]
    assert url == "https://acct123.r2.cloudflarestorage.com/trip-photos/trips/b1/x.png" and kw["content"] == PNG
    auth = kw["headers"]["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIATESTKEY/20261012/auto/s3/aws4_request, SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, Signature=")
    assert kw["headers"]["x-amz-content-sha256"] == hashlib.sha256(PNG).hexdigest() and R2["R2_SECRET_ACCESS_KEY"] not in str(calls)
    monkeypatch.setattr(httpx, "put", lambda url, **kw: SimpleNamespace(status_code=403))
    with pytest.raises(media_storage.StorageError):
        media_storage.put_object("k", b"x", "image/png")

    def boom(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "put", boom)
    with pytest.raises(media_storage.StorageError):
        media_storage.put_object("k", b"x", "image/png")


# ---- endpoints --------------------------------------------------------------------------------------------------------


@pytest.fixture()
def env(monkeypatch):
    env = t.build_env(monkeypatch, media.router)
    env.puts = []
    monkeypatch.setattr(httpx, "put", lambda url, **kw: env.puts.append((url, kw)) or SimpleNamespace(status_code=200))
    t.add_so(env, "s1", batch="b1", driver=9, helpers=(17,))
    t.add_so(env, "s2", batch="b2", driver=10)
    ids = {"driver": t.make_user(env, name="Emert", email="emert@example.test", role="driver", staff_id=9), "helper": t.make_user(env, name="Raymond", email="ray@example.test", role="helper", staff_id=17),
           "other": t.make_user(env, name="Paul", email="paul@example.test", role="driver", staff_id=10), "officer": t.make_user(env, name="Olivia", email="o@example.test", role="logistics_officer"),
           "admin": t.make_user(env, name="Ada", email="ada@example.test", role="admin"), "dispatcher": t.make_user(env, name="Dee", email="dee@example.test", role="dispatcher"),
           "client": t.make_user(env, name="Cleo", email="cleo@example.test", role="client")}
    roles = {"other": "driver"}
    env.ids = ids
    env.h = {name: t.bearer(uid, roles.get(name, name if name != "officer" else "logistics_officer")) for name, uid in ids.items()}
    yield env
    assert env.zoho_calls == []
    env.engine.dispose()


def enable(monkeypatch):
    for name, value in R2.items():
        monkeypatch.setenv(name, value)


def upload(env, who, data=JPEG, *, batch="b1", key="u-1", so="s1", name="photo.jpg", field="delivery_photo"):
    form = {"batch_id": batch, "field_key": field, "upload_key": key}
    if so:
        form["so_id"] = so
    return env.client.post("/api/driver/uploads", headers=env.h[who], files={"file": (name, data, "image/jpeg")}, data=form)


def test_upload_returns_503_storage_disabled_when_r2_is_not_configured(env, monkeypatch):
    for name in R2:
        monkeypatch.delenv(name, raising=False)
    response = upload(env, "driver")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "storage_disabled" and env.puts == []
    assert env.client.get("/api/media/1", headers=env.h["admin"]).status_code in (404, 503)


def test_upload_stores_one_metadata_row_and_is_idempotent_by_upload_key(env, monkeypatch):
    enable(monkeypatch)
    first = upload(env, "driver")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["content_type"] == "image/jpeg" and body["size_bytes"] == len(JPEG) and body["photo_id"] >= 1 and body["replayed"] is False
    again = upload(env, "driver", data=JPEG + b"different bytes")  # a retry of the same upload_key (the phone cannot know if the first arrived)
    assert again.status_code == 200 and again.json()["photo_id"] == body["photo_id"] and again.json()["replayed"] is True
    assert len(env.puts) == 1  # bytes were sent to storage once
    with env.Session() as db:
        rows = db.execute(select(TripPhoto)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.batch_id == "b1" and row.so_id == "s1" and row.field_key == "delivery_photo" and row.uploaded_by_user_id == env.ids["driver"] and row.upload_key == "u-1"
        assert row.storage_key.startswith("trips/b1/") and row.storage_key.endswith(".jpg") and "photo" not in row.storage_key  # never the user's filename
    url, kw = env.puts[0]
    assert url.endswith("/" + row.storage_key) and kw["headers"]["content-type"] == "image/jpeg"
    other_key = upload(env, "driver", key="u-2", data=PNG, name="x.exe")
    assert other_key.status_code == 200 and other_key.json()["content_type"] == "image/png" and other_key.json()["photo_id"] != body["photo_id"]  # type from magic bytes, name ignored
    assert upload(env, "driver", key="u-3", data=WEBP).json()["content_type"] == "image/webp"
    assert upload(env, "driver", key="u-4", so=None).status_code == 200  # so_id is optional
    assert upload(env, "helper", key="u-1").status_code == 409  # someone else's key


def test_crew_helper_may_upload_but_outsiders_and_other_roles_may_not(env, monkeypatch):
    enable(monkeypatch)
    assert upload(env, "helper", key="h-1").status_code == 200
    assert upload(env, "other", key="o-1").status_code == 404  # not on batch b1: same answer as "does not exist"
    assert upload(env, "driver", key="n-1", batch="nope").status_code == 404
    for who in ("officer", "admin", "dispatcher", "client"):
        assert upload(env, who, key=f"r-{who}").status_code == 403
    assert env.client.post("/api/driver/uploads", files={"file": ("a.jpg", JPEG, "image/jpeg")}, data={"batch_id": "b1", "field_key": "f", "upload_key": "z"}).status_code == 401
    assert upload(env, "driver", key="s-1", so="s2").status_code == 422  # an SO of another batch
    assert env.puts and len(env.puts) == 1  # only the helper's upload reached storage


def test_upload_rejects_non_images_and_oversize_files(env, monkeypatch):
    enable(monkeypatch)
    assert upload(env, "driver", data=b"<html>not an image</html>", key="v-1", name="x.jpg").status_code == 422
    assert upload(env, "driver", data=b"%PDF-1.4 ...", key="v-2").status_code == 422
    assert upload(env, "driver", data=b"RIFF\x00\x00\x00\x00WAVEfmt ", key="v-3").status_code == 422  # RIFF but not WEBP
    too_big = upload(env, "driver", data=JPEG + b"\x00" * (5 * 1024 * 1024), key="v-4")
    assert too_big.status_code == 413
    exactly = upload(env, "driver", data=JPEG + b"\x00" * (5 * 1024 * 1024 - len(JPEG)), key="v-5")
    assert exactly.status_code == 200
    assert env.client.post("/api/driver/uploads", headers=env.h["driver"], data={"batch_id": "b1", "field_key": "f", "upload_key": "v-6"}).status_code == 422  # no file
    assert len(env.puts) == 1
    with env.Session() as db:
        assert [r.upload_key for r in db.execute(select(TripPhoto)).scalars().all()] == ["v-5"]


def test_storage_failure_stores_no_row(env, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(httpx, "put", lambda url, **kw: SimpleNamespace(status_code=500))
    assert upload(env, "driver", key="f-1").status_code == 503
    with env.Session() as db:
        assert db.execute(select(TripPhoto)).first() is None


def test_media_redirect_is_for_crew_officer_admin_dispatcher_only(env, monkeypatch):
    enable(monkeypatch)
    photo_id = upload(env, "driver").json()["photo_id"]
    for who in ("driver", "helper", "officer", "admin", "dispatcher"):
        response = env.client.get(f"/api/media/{photo_id}", headers=env.h[who], follow_redirects=False)
        assert response.status_code == 302, who
        location = response.headers["location"]
        assert location.startswith("https://acct123.r2.cloudflarestorage.com/trip-photos/trips/b1/") and "X-Amz-Expires=600" in location and "X-Amz-Signature=" in location
        assert R2["R2_SECRET_ACCESS_KEY"] not in location
    assert env.client.get(f"/api/media/{photo_id}", headers=env.h["other"], follow_redirects=False).status_code == 404  # a driver of another batch
    assert env.client.get(f"/api/media/{photo_id}", headers=env.h["client"], follow_redirects=False).status_code == 403
    assert env.client.get(f"/api/media/{photo_id}", follow_redirects=False).status_code == 401
    assert env.client.get("/api/media/9999", headers=env.h["admin"], follow_redirects=False).status_code == 404
    monkeypatch.delenv("R2_BUCKET")
    assert env.client.get(f"/api/media/{photo_id}", headers=env.h["admin"], follow_redirects=False).status_code == 503
