"""Google Drive photo storage: upload (re-encode, dated folder, naming, description, appProperties, idempotency, errors),
post-check-out / post-issue Drive updates, streaming viewer (ETag / 304 / thumb), provider switch, legacy R2 keys.
A fake Drive answers through httpx.MockTransport: zero network, no Google SDK."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from models.trip import FormTemplateItem, TripPhoto
from routers import driver_extra, driver_trip, media
from services import gdrive_storage, media_storage, trip_events
from tests import trip_test_support as t

MANILA = ZoneInfo("Asia/Manila")
ROOT = "root-folder-id"
REFRESH = "refresh-token-not-real-123"
ACCESS = "access-token-not-real-456"
GDRIVE_ENV = {"MEDIA_STORAGE": "gdrive", "GDRIVE_PHOTO_FOLDER_ID": ROOT, "GDRIVE_REFRESH_TOKEN": REFRESH,
              "GMAIL_COMMS_CLIENT_ID": "client-id.apps.googleusercontent.com", "GMAIL_COMMS_CLIENT_SECRET": "client-secret-not-real"}
R2 = {"R2_ACCOUNT_ID": "acct123", "R2_ACCESS_KEY_ID": "AKIATESTKEY", "R2_SECRET_ACCESS_KEY": "test-secret-value-not-real", "R2_BUCKET": "trip-photos"}


# ---- a tiny fake Google Drive ---------------------------------------------------------------------------------------


class FakeDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, list[int]] = {}  # route -> statuses to answer with first (token, upload, patch, list, media, meta)
        self.token_calls = 0
        self.seq = 0

    def _fail(self, route):
        queue = self.fail.get(route)
        if queue:
            status = queue.pop(0)
            reason = "rateLimitExceeded" if status == 403 else "backendError"
            return httpx.Response(status, json={"error": {"code": status, "errors": [{"reason": reason}]}})
        return None

    def _new(self, **fields):
        self.seq += 1
        file_id = f"f{self.seq:03d}"
        content = fields.pop("content", b"")
        self.files[file_id] = {"id": file_id, "trashed": False, "createdTime": f"2026-10-15T00:00:{self.seq:02d}Z", "content": content,
                               "md5Checksum": hashlib.md5(content).hexdigest() if content else None, "appProperties": {}, "description": "", **fields}
        return self.files[file_id]

    @staticmethod
    def public(f):
        return {k: v for k, v in f.items() if k not in ("content", "trashed")}

    def folders(self, parent=ROOT):
        return [f for f in self.files.values() if f.get("mimeType") == gdrive_storage.FOLDER_MIME and parent in f.get("parents", [])]

    def photos(self):
        return [f for f in self.files.values() if f.get("mimeType") != gdrive_storage.FOLDER_MIME]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append((request.method, url.path))
        if url.netloc == "oauth2.googleapis.com":
            self.token_calls += 1
            body = parse_qs(request.content.decode())
            assert body["refresh_token"] == [REFRESH] and body["grant_type"] == ["refresh_token"]
            failed = self.fail.get("token")
            if failed:
                status = failed.pop(0)
                return httpx.Response(status, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3599, "token_type": "Bearer"})
        assert request.headers["Authorization"] == f"Bearer {ACCESS}"
        if url.path == "/upload/drive/v3/files" and request.method == "POST":
            if (failed := self._fail("upload")) is not None:
                return failed
            assert params["uploadType"] == "multipart" and params["supportsAllDrives"] == "true"
            boundary = re.search(r"boundary=(.+)$", request.headers["Content-Type"]).group(1).encode()
            parts = request.content.split(b"--" + boundary)
            meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].strip())
            content = parts[2].split(b"\r\n\r\n", 1)[1][:-2]
            return httpx.Response(200, json=self.public(self._new(content=content, **meta)))
        if url.path == "/drive/v3/files" and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(200, json={"id": self._new(**body)["id"]})
        if url.path == "/drive/v3/files" and request.method == "GET":
            if (failed := self._fail("list")) is not None:
                return failed
            return httpx.Response(200, json={"files": [self.public(f) for f in self.query(params["q"])]})
        match = re.fullmatch(r"/drive/v3/files/([^/]+)", url.path)
        if match:
            f = self.files.get(match.group(1))
            if f is None:
                return httpx.Response(404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}})
            if request.method == "PATCH":
                if (failed := self._fail("patch")) is not None:
                    return failed
                f.update(json.loads(request.content))
                return httpx.Response(200, json=self.public(f))
            if params.get("alt") == "media":
                if (failed := self._fail("media")) is not None:
                    return failed
                return httpx.Response(200, content=f["content"], headers={"Content-Type": "image/jpeg"})
            return httpx.Response(200, json=self.public(f))
        raise AssertionError(f"unexpected Drive call {request.method} {url.path}")

    def query(self, q: str) -> list[dict]:
        live = [f for f in self.files.values() if not f["trashed"]]
        prop = re.search(r"appProperties has \{ key='(.+?)' and value='(.+?)' \}", q)
        if prop:
            return [f for f in live if (f.get("appProperties") or {}).get(prop.group(1)) == prop.group(2)]
        folder = re.match(r"name = '(.+?)' and '(.+?)' in parents and mimeType", q)
        if folder:
            return [f for f in live if f.get("name") == folder.group(1) and folder.group(2) in f.get("parents", []) and f.get("mimeType") == gdrive_storage.FOLDER_MIME]
        names = re.match(r"'(.+?)' in parents and trashed = false and \((.*)\)$", q)
        if names:
            wanted = {n.replace("\\'", "'").replace("\\\\", "\\") for n in re.findall(r"name = '((?:[^'\\]|\\.)*)'", names.group(2))}
            return [f for f in live if names.group(1) in f.get("parents", []) and f.get("name") in wanted]
        raise AssertionError(f"unexpected q {q}")


# ---- fixtures ---------------------------------------------------------------------------------------------------------


@pytest.fixture()
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(gdrive_storage, "_transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(gdrive_storage, "_sleep", lambda s: None)
    monkeypatch.setattr(media_storage, "_spawn", lambda fn: fn())  # the "off the request" Drive updates run inline in tests
    for name in R2:
        monkeypatch.delenv(name, raising=False)
    for name in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in GDRIVE_ENV.items():
        monkeypatch.setenv(name, value)
    gdrive_storage.reset_cache()
    media_storage.reset_caches()
    yield fake
    gdrive_storage.reset_cache()
    media_storage.reset_caches()


@pytest.fixture()
def env(monkeypatch, drive):
    env = t.build_env(monkeypatch, media.router, driver_trip.router, driver_extra.router)
    t.seed_templates(env)
    with env.Session() as db:  # the production checkout form has an Invoice / DR number item; the seed migration predates it
        db.add(FormTemplateItem(template_id=2, position=99, key="invoice_dr_number", label="Invoice / DR number", item_type="numeric_text", required=False, active=True))
        db.commit()
    t.add_so(env, "s1", number="SO26-18449", customer="Jollibee Makati", batch="b1", driver=9, helpers=(17,), vehicle="NAA1234")
    t.add_so(env, "s2", number="SO26-18450", customer="Mang Inasal", batch="b1", driver=9, helpers=(17,), vehicle="NAA1234", offset=1)
    t.add_so(env, "TO:77", number="TO-00123", customer="Glacier Warehouse", batch="b1", driver=9, helpers=(17,), vehicle="NAA1234", offset=2)
    t.add_so(env, "x1", batch="b2", driver=10)
    env.ids = {"driver": t.make_user(env, name="Emert", email="emert@example.test", role="driver", staff_id=9),
               "helper": t.make_user(env, name="Raymond", email="ray@example.test", role="helper", staff_id=17),
               "other": t.make_user(env, name="Paul", email="paul@example.test", role="driver", staff_id=10),
               "admin": t.make_user(env, name="Ada", email="ada@example.test", role="admin")}
    env.h = {name: t.bearer(uid, "driver" if name == "other" else name) for name, uid in env.ids.items()}
    env.drive = drive
    yield env
    assert env.zoho_calls == []
    env.engine.dispose()


def photo_bytes(size=(3000, 2000), fmt="JPEG", gps=True, orientation=None, color=(200, 30, 30)) -> bytes:
    image = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    if orientation:
        exif[0x0112] = orientation
    if gps:
        exif.get_ifd(0x8825).update({1: "N", 2: (14.0, 33.0, 12.5), 3: "E", 4: (121.0, 1.0, 30.0)})
    out = io.BytesIO()
    image.save(out, format=fmt, exif=exif.tobytes())
    return out.getvalue()


SMALL = None


def small() -> bytes:
    global SMALL
    if SMALL is None:
        SMALL = photo_bytes((640, 480))
    return SMALL


def upload(env, who="driver", *, data=None, batch="b1", so="s1", field="delivery_photo", key=None, captured=None):
    form = {"batch_id": batch, "field_key": field, "upload_key": key or f"k-{uuid.uuid4().hex[:8]}"}
    if so:
        form["so_id"] = so
    if captured is not None:
        form["captured_at"] = captured.isoformat()
    return env.client.post("/api/driver/uploads", headers=env.h[who], files={"file": ("IMG_0001.jpg", data or small(), "image/jpeg")}, data=form)


def now_minute() -> datetime:
    return datetime.now(timezone.utc).replace(second=5, microsecond=0) - timedelta(minutes=2)


def stamp(captured: datetime) -> str:
    local = captured.astimezone(MANILA)
    return f"{local:%Y-%m-%d}_{local:%H-%M}"


def row_of(env, photo_id) -> TripPhoto:
    with env.Session() as db:
        return db.get(TripPhoto, photo_id)


def file_of(env, photo_id) -> dict:
    key = row_of(env, photo_id).storage_key
    assert key.startswith("gdrive:")
    return env.drive.files[key[len("gdrive:"):]]


# ---- upload ---------------------------------------------------------------------------------------------------------


def test_upload_stores_one_reencoded_file_in_the_dated_folder_with_name_description_and_app_properties(env, caplog):
    caplog.set_level(logging.DEBUG)
    captured = now_minute()
    response = upload(env, key="up-1", captured=captured)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"photo_id", "content_type", "size_bytes", "replayed"} and body["replayed"] is False and body["content_type"] == "image/jpeg"
    photos = env.drive.photos()
    assert len(photos) == 1
    f = photos[0]
    day = f"{captured.astimezone(MANILA):%Y-%m-%d}"
    folders = env.drive.folders()
    assert [d["name"] for d in folders] == [day] and f["parents"] == [folders[0]["id"]]
    assert f["name"] == f"SO26-18449_{stamp(captured)}_delivery.jpg"
    local = captured.astimezone(MANILA)
    assert f["description"].split("\n") == ["SO: SO26-18449", f"Date: {local:%Y-%m-%d}", f"Time: {local:%H:%M} (Manila)", "Customer: Jollibee Makati", "Truck: NAA1234",
                                             "Driver: Emert Hirondo    Helper: Raymond Roblo", "Trip: b1"]
    assert f["appProperties"] == {"upload_key": "up-1", "batch_id": "b1", "so_id": "s1", "so_number": "SO26-18449", "field_key": "delivery_photo",
                                  "captured_at": captured.isoformat(timespec="seconds"), "app": "intellifleet"}
    row = row_of(env, body["photo_id"])
    assert row.storage_key == f"gdrive:{f['id']}" and row.content_type == "image/jpeg" and row.size_bytes == len(f["content"]) == body["size_bytes"]
    assert "IMG_0001" not in f["name"]
    logged = caplog.text + json.dumps([list(c) for c in env.drive.calls])
    assert REFRESH not in logged and ACCESS not in logged and GDRIVE_ENV["GMAIL_COMMS_CLIENT_SECRET"] not in logged
    assert not any(c[1].endswith("/permissions") for c in env.drive.calls)  # never an "anyone with the link" permission


def test_an_offline_queued_photo_is_named_and_filed_by_its_capture_time_not_its_receipt(env):
    captured = (datetime.now(timezone.utc) - timedelta(hours=30)).replace(microsecond=0)  # taken yesterday, sent today
    response = upload(env, field="failure_photo", so="s2", captured=captured)
    assert response.status_code == 200
    f = env.drive.photos()[0]
    assert f["name"] == f"SO26-18450_{stamp(captured)}_failed.jpg"
    assert env.drive.files[f["parents"][0]]["name"] == f"{captured.astimezone(MANILA):%Y-%m-%d}"
    assert "Result: Not delivered" in f["description"].split("\n")
    assert f["appProperties"]["captured_at"] == captured.isoformat(timespec="seconds")
    stored = row_of(env, response.json()["photo_id"])
    assert stored.created_at.replace(tzinfo=timezone.utc) == captured
    captions = [{"photo_id": stored.id, "captured_at": "wrong action timestamp"}]
    from services import day_model
    with env.Session() as db:
        day_model.photo_capture_times(db, captions)
    assert datetime.fromisoformat(captions[0]["captured_at"]) == captured


def test_concurrent_upload_key_retries_create_one_drive_file(drive):
    from concurrent.futures import ThreadPoolExecutor

    meta = media_storage.PhotoMeta(batch_id="b1", so_id="s1", field_key="delivery_photo", order_number="SO-1", kind="delivery")
    captured = now_minute()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(media_storage.store_drive_photo, meta, "same-retry-key", captured, small()) for _ in range(2)]
        files = [future.result() for future in futures]
    assert files[0]["id"] == files[1]["id"]
    assert len(drive.photos()) == 1


def test_no_capture_time_falls_back_to_the_receipt_time(env, monkeypatch):
    received = datetime(2026, 10, 15, 5, 42, 30, tzinfo=timezone.utc)  # 13:42 Manila
    monkeypatch.setattr(trip_events, "now_utc", lambda: received)
    assert upload(env).status_code == 200
    assert env.drive.photos()[0]["name"] == "SO26-18449_2026-10-15_13-42_delivery.jpg"


def test_two_photos_of_one_order_in_the_same_minute_share_the_day_folder_and_get_a_suffix(env):
    captured = now_minute()
    first = upload(env, captured=captured)
    second = upload(env, captured=captured + timedelta(seconds=20))
    assert first.status_code == second.status_code == 200
    names = sorted(f["name"] for f in env.drive.photos())
    base = f"SO26-18449_{stamp(captured)}_delivery"
    assert names == [f"{base}.jpg", f"{base}_2.jpg"]
    assert len(env.drive.folders()) == 1  # the day folder is created once
    gdrive_storage.reset_cache()  # another instance (empty folder cache) finds the folder by name instead of creating a second one
    assert upload(env, captured=captured).status_code == 200
    assert len(env.drive.folders()) == 1 and sorted(f["name"] for f in env.drive.photos())[-1] == f"{base}_3.jpg"


def test_a_name_with_unsafe_characters_is_sanitised():
    assert media_storage.sanitize_name('SO/26\\1:8*4?"<4>|9_x') == "SO-26-1-8-4-4-9_x"


def test_exif_gps_is_stripped_orientation_applied_and_the_longest_side_is_at_most_1600(env):
    data = photo_bytes((3000, 2000), orientation=6)  # landscape pixels, "rotate 90" tag: a portrait photo
    assert b"PhoneMaker" in data
    assert upload(env, data=data).status_code == 200
    stored = env.drive.photos()[0]["content"]
    image = Image.open(io.BytesIO(stored))
    assert image.format == "JPEG" and max(image.size) <= 1600 and image.size[1] > image.size[0]
    exif = image.getexif()
    assert len(exif) == 0 and not exif.get_ifd(0x8825) and b"PhoneMaker" not in stored and b"Exif" not in stored[:200]
    png = io.BytesIO()
    Image.new("RGBA", (500, 400), (0, 0, 255, 0)).save(png, format="PNG")
    assert upload(env, data=png.getvalue()).status_code == 200  # PNG / WebP are accepted and stored as JPEG
    assert Image.open(io.BytesIO(env.drive.photos()[-1]["content"])).format == "JPEG"


def test_a_transfer_order_photo_uses_the_to_number(env):
    captured = now_minute()
    assert upload(env, so="TO:77", captured=captured).status_code == 200
    f = env.drive.photos()[0]
    assert f["name"] == f"TO-00123_{stamp(captured)}_delivery.jpg"
    lines = f["description"].split("\n")
    assert lines[0] == "TO: TO-00123" and "TO#: TO-00123" in lines and not any(line.startswith("Invoice/DR") for line in lines)


def test_replaying_an_upload_key_never_creates_a_second_file(env):
    first = upload(env, key="same-key")
    again = upload(env, key="same-key")
    assert again.status_code == 200 and again.json()["replayed"] is True and again.json()["photo_id"] == first.json()["photo_id"]
    assert len(env.drive.photos()) == 1
    file_id = env.drive.photos()[0]["id"]
    with env.Session() as db:  # an earlier attempt uploaded but crashed before its DB insert: the file exists, the row does not
        db.delete(db.get(TripPhoto, first.json()["photo_id"]))
        db.commit()
    third = upload(env, key="same-key")
    assert third.status_code == 200 and third.json()["replayed"] is False
    assert len(env.drive.photos()) == 1 and row_of(env, third.json()["photo_id"]).storage_key == f"gdrive:{file_id}"


def test_an_expired_token_answers_503_and_stores_nothing(env, caplog):
    caplog.set_level(logging.INFO)
    env.drive.fail["token"] = [400]
    response = upload(env)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "storage_disabled"
    assert env.drive.photos() == []
    with env.Session() as db:
        assert db.execute(select(TripPhoto)).first() is None
    assert "Drive token expired/revoked" in caplog.text and REFRESH not in caplog.text and ACCESS not in caplog.text
    assert upload(env).status_code == 200  # the token works again (re-authorised): the queued photo goes through


def test_a_rate_limit_or_5xx_is_retried_once_then_503(env):
    env.drive.fail["upload"] = [503]
    assert upload(env).status_code == 200 and len(env.drive.photos()) == 1
    env.drive.fail["upload"] = [403, 500]
    assert upload(env).status_code == 503 and len(env.drive.photos()) == 1
    env.drive.fail["list"] = [429, 429]
    assert upload(env).status_code == 503 and len(env.drive.photos()) == 1
    with env.Session() as db:
        assert len(db.execute(select(TripPhoto)).scalars().all()) == 1


def test_crew_scope_and_magic_bytes_still_apply(env):
    assert upload(env, "other").status_code == 404
    assert upload(env, "helper").status_code == 200
    assert upload(env, data=b"<html>nope</html>").status_code == 422
    assert upload(env, data=b"\xff\xd8\xff\xe0 truncated jpeg").status_code == 422  # magic bytes ok, not decodable
    assert upload(env, data=b"\xff\xd8\xff" + b"\x00" * (5 * 1024 * 1024)).status_code == 413
    assert len(env.drive.photos()) == 1


# ---- after check-out / issue ------------------------------------------------------------------------------------------


def body(**extra):
    return {"idempotency_key": str(uuid.uuid4()), "client_ts": None, "gps": None, "gps_denied_reason": "denied", **extra}


def on_trip(env, sos=("s1", "s2", "TO:77")):
    trip_id = t.add_trip(env, "b1", status="on_trip", driver=9, helpers=(17,), started=datetime.now(timezone.utc) - timedelta(hours=1))
    for seq, so in enumerate(sos, 1):
        t.add_stop(env, trip_id, so, seq, status="checked_in", checkin=datetime.now(timezone.utc) - timedelta(minutes=10), user_id=env.ids["driver"])
    return trip_id


def check_out(env, so, outcome, **answers):
    base = {"outcome": outcome}
    if outcome in ("Delivered", "Partially delivered"):
        base["received_by"] = "Ana"
    if outcome != "Delivered":
        base["reason"] = "short"
    base.update(answers)
    return env.client.post(f"/api/driver/batches/b1/orders/{so}/check-out", headers=env.h["driver"], json=body(answers=base))


def test_after_the_check_out_commits_the_drive_name_and_description_get_the_result_and_invoice(env):
    on_trip(env)
    captured = now_minute()
    photo_id = upload(env, captured=captured).json()["photo_id"]
    assert file_of(env, photo_id)["name"].endswith("_delivery.jpg")
    response = check_out(env, "s1", "Partially delivered", invoice_dr_number="004512", delivery_photo=photo_id)
    assert response.status_code == 200, response.text
    delivered_order = next(o for o in response.json()["batch"]["orders"] if o["id"] == "s1")
    caption = delivered_order["workflow"]["stop"]["photos"][0]
    assert caption["photo_id"] == photo_id and datetime.fromisoformat(caption["captured_at"]) == captured
    f = file_of(env, photo_id)
    assert f["name"] == f"SO26-18449_{stamp(captured)}_partial.jpg"
    lines = f["description"].split("\n")
    assert "Result: Partial" in lines and "Invoice/DR: 004512" in lines and f"Time: {captured.astimezone(MANILA):%H:%M} (Manila)" in lines
    patches = [c for c in env.drive.calls if c[0] == "PATCH"]
    assert len(patches) == 1
    assert check_out(env, "s1", "Partially delivered", invoice_dr_number="004512", delivery_photo=photo_id).status_code == 200  # already done: no second update
    assert len([c for c in env.drive.calls if c[0] == "PATCH"]) == 1

    delivered = upload(env, so="s2", captured=captured).json()["photo_id"]
    assert check_out(env, "s2", "Delivered", invoice_dr_number="7788", delivery_photo=delivered).status_code == 200
    assert file_of(env, delivered)["name"] == f"SO26-18450_{stamp(captured)}_delivered.jpg" and "Result: Delivered" in file_of(env, delivered)["description"]


def test_a_transfer_order_check_out_names_the_photo_with_the_to_number(env):
    on_trip(env)
    captured = now_minute()
    photo_id = upload(env, so="TO:77", captured=captured).json()["photo_id"]
    assert check_out(env, "TO:77", "Delivered", delivery_photo=photo_id).status_code == 200
    f = file_of(env, photo_id)
    assert f["name"] == f"TO-00123_{stamp(captured)}_delivered.jpg"
    lines = f["description"].split("\n")
    assert "TO#: TO-00123" in lines and "Result: Delivered" in lines and not any(line.startswith("Invoice/DR") for line in lines)


def test_a_failed_drive_update_never_fails_the_check_out_and_is_retried_on_the_next_view(env):
    on_trip(env)
    photo_id = upload(env, so="s2").json()["photo_id"]
    env.drive.fail["patch"] = [500, 500]  # the update and its one retry both fail
    response = check_out(env, "s2", "Delivered", invoice_dr_number="55", delivery_photo=photo_id)
    assert response.status_code == 200
    assert media_storage.pending_count() == 1 and file_of(env, photo_id)["name"].endswith("_delivery.jpg")
    assert env.client.get(f"/api/media/{photo_id}?size=thumb", headers=env.h["admin"]).status_code == 200
    f = file_of(env, photo_id)
    assert media_storage.pending_count() == 0 and f["name"].endswith("_delivered.jpg") and "Invoice/DR: 55" in f["description"].splitlines()


def test_a_failure_photo_keeps_failed_and_gets_no_invoice_line(env):
    on_trip(env)
    photo_id = upload(env, field="failure_photo", so="s2").json()["photo_id"]
    assert check_out(env, "s2", "Failed / Returned").status_code == 200
    f = file_of(env, photo_id)
    assert f["name"].endswith("_failed.jpg") and "Result: Not delivered" in f["description"].splitlines()
    assert not any(line.startswith("Invoice") for line in f["description"].splitlines())


def test_an_issue_report_adds_the_reference_and_category_to_its_photos(env):
    captured = now_minute()
    photo_id = upload(env, field="issue_photo", so="s1", captured=captured).json()["photo_id"]
    f = file_of(env, photo_id)
    assert f["name"] == f"SO26-18449_{stamp(captured)}_problem.jpg" and "Result: Problem" in f["description"].split("\n")
    response = env.client.post("/api/driver/issues", headers=env.h["driver"], json={"idempotency_key": "issue-key-0001", "category": "Damaged goods", "priority": "high",
                                                                                     "description": "Two boxes crushed on arrival", "batch_id": "b1", "so_id": "s1", "photo_ids": [photo_id]})
    assert response.status_code == 200, response.text
    reference = response.json()["issue"]["reference"]
    lines = file_of(env, photo_id)["description"].split("\n")
    assert reference.startswith("IS-") and f"Issue: {reference}" in lines and "Result: Problem: Damaged goods" in lines
    assert file_of(env, photo_id)["name"] == f"SO26-18449_{stamp(captured)}_problem.jpg"


# ---- viewing ----------------------------------------------------------------------------------------------------------


def test_media_streams_the_bytes_with_cache_headers_etag_304_and_a_thumbnail(env):
    photo_id = upload(env, data=photo_bytes((2400, 1800))).json()["photo_id"]
    f = file_of(env, photo_id)
    media_storage.reset_caches()  # a fresh instance: the bytes come from Drive (files.get?alt=media)
    full = env.client.get(f"/api/media/{photo_id}", headers=env.h["admin"], follow_redirects=False)
    assert full.status_code == 200 and full.headers["content-type"] == "image/jpeg" and full.headers["cache-control"] == "private, max-age=86400"
    assert full.content == f["content"] and full.headers["etag"] == f'"{f["id"]}-{f["md5Checksum"]}"'
    assert sum(1 for c in env.drive.calls if c == ("GET", f"/drive/v3/files/{f['id']}")) == 1
    again = env.client.get(f"/api/media/{photo_id}?size=full", headers=env.h["driver"])
    assert again.status_code == 200 and sum(1 for c in env.drive.calls if c == ("GET", f"/drive/v3/files/{f['id']}")) == 1  # LRU hit
    not_modified = env.client.get(f"/api/media/{photo_id}", headers={**env.h["admin"], "If-None-Match": full.headers["etag"]})
    assert not_modified.status_code == 304 and not_modified.content == b""
    thumb = env.client.get(f"/api/media/{photo_id}?size=thumb", headers=env.h["helper"])
    assert thumb.status_code == 200 and thumb.headers["etag"] != full.headers["etag"]
    assert max(Image.open(io.BytesIO(thumb.content)).size) <= 320
    media_storage.reset_caches()
    revalidated = env.client.get(f"/api/media/{photo_id}?size=thumb", headers={**env.h["admin"], "If-None-Match": thumb.headers["etag"]})
    assert revalidated.status_code == 304  # one metadata call, no download
    assert env.client.get(f"/api/media/{photo_id}?size=huge", headers=env.h["admin"]).status_code == 422


def test_media_crew_and_role_rules_are_unchanged(env):
    photo_id = upload(env).json()["photo_id"]
    assert env.client.get(f"/api/media/{photo_id}", headers=env.h["other"]).status_code == 404  # another crew: same answer as "does not exist"
    assert env.client.get(f"/api/media/{photo_id}").status_code == 401
    env.drive.files[file_of(env, photo_id)["id"]]["trashed"] = True
    del env.drive.files[file_of(env, photo_id)["id"]]
    media_storage.reset_caches()
    assert env.client.get(f"/api/media/{photo_id}", headers=env.h["admin"]).status_code == 404


def test_media_storage_none_keeps_photo_not_configured(env, monkeypatch):
    monkeypatch.setenv("MEDIA_STORAGE", "none")
    assert media_storage.storage_available() is False and media_storage.is_enabled() is False
    response = upload(env)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "storage_disabled" and env.drive.calls == []
    monkeypatch.setenv("MEDIA_STORAGE", "gdrive")
    monkeypatch.delenv("GDRIVE_REFRESH_TOKEN")
    assert media_storage.provider() == "none" and upload(env).status_code == 503
    monkeypatch.delenv("MEDIA_STORAGE")
    assert media_storage.provider() == "none"
    monkeypatch.setenv("GDRIVE_REFRESH_TOKEN", REFRESH)
    assert media_storage.provider() == "gdrive"  # default when the Drive env is complete
    monkeypatch.setenv("GDRIVE_CLIENT_ID", "override-id")
    monkeypatch.setenv("GDRIVE_CLIENT_SECRET", "override-secret")
    assert gdrive_storage.client_id() == "override-id" and gdrive_storage.client_secret() == "override-secret"


def test_old_r2_rows_still_resolve_through_r2(env, monkeypatch):
    with env.Session() as db:
        db.add(TripPhoto(batch_id="b1", so_id="s1", field_key="delivery_photo", storage_key="trips/b1/old.jpg", content_type="image/jpeg", size_bytes=10, uploaded_by_user_id=env.ids["driver"], upload_key="old"))
        db.commit()
        photo_id = db.execute(select(TripPhoto.id).where(TripPhoto.upload_key == "old")).scalar_one()
    assert media_storage.provider() == "gdrive"
    assert env.client.get(f"/api/media/{photo_id}", headers=env.h["admin"], follow_redirects=False).status_code == 503  # R2 not configured
    for name, value in R2.items():
        monkeypatch.setenv(name, value)
    response = env.client.get(f"/api/media/{photo_id}", headers=env.h["admin"], follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"].startswith("https://acct123.r2.cloudflarestorage.com/trip-photos/trips/b1/old.jpg?")
    assert media_storage.provider() == "gdrive"  # both configured: new photos still go to Drive
    monkeypatch.setenv("MEDIA_STORAGE", "r2")
    assert media_storage.provider() == "r2"
