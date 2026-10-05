import base64
import email
from email import policy

import pytest

from services import gmail_sender


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = str(self._body)

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def gmail_env(monkeypatch):
    monkeypatch.setenv("GMAIL_COMMS_CLIENT_ID", "id")
    monkeypatch.setenv("GMAIL_COMMS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_COMMS_REFRESH_TOKEN", "refresh")
    for name in ("GMAIL_FROM_ADDRESS", "GMAIL_FROM_NAME", "GMAIL_REPLY_TO"):
        monkeypatch.delenv(name, raising=False)
    gmail_sender._token.update(value="tok", expires_at=9e12)
    gmail_sender._mailbox["address"] = "dispatch@example.com"
    gmail_sender._sent_keys.clear()
    gmail_sender._log.clear()
    gmail_sender._sendas.update(addresses=None, fetched_at=0.0)
    gmail_sender._labels.update(ids={"Logistics": "L1", "Logistics/Sent": "L2"}, fetched_at=9e12)


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        if url != gmail_sender.SEND_URL:
            return FakeResponse(200, {"id": "label-call"})
        calls.append(kwargs["json"]["raw"])
        return FakeResponse(200, {"id": f"m{len(calls)}", "threadId": "t1"})

    monkeypatch.setattr(gmail_sender.httpx, "post", fake_post)
    return calls


def parse(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def test_send_builds_identity_headers_and_utf8(monkeypatch, sent):
    monkeypatch.setenv("GMAIL_FROM_ADDRESS", "martin.logistics@rareglobalfood.com")
    monkeypatch.setenv("GMAIL_FROM_NAME", "Martin Cuico")
    monkeypatch.setenv("GMAIL_REPLY_TO", "martin.logistics@rareglobalfood.com")
    result = gmail_sender.send_email(to=["a@x.com", "b@x.com"], cc="c@x.com", subject="Assignment ñ – Truck", html="<p>Café ✓</p>", purpose="t")
    msg = parse(sent[0])
    assert result["id"] == "m1"
    assert msg["From"] == "Martin Cuico <martin.logistics@rareglobalfood.com>"
    assert msg["Reply-To"] == "martin.logistics@rareglobalfood.com"
    assert msg["To"] == "a@x.com, b@x.com"
    assert msg["Cc"] == "c@x.com"
    assert msg["Subject"] == "Assignment ñ – Truck"
    assert "Café ✓" in msg.get_body(("html",)).get_content()


def test_defaults_use_authenticated_mailbox_without_reply_to(sent):
    gmail_sender.send_email(to="a@x.com", subject="s", html="<p>x</p>")
    msg = parse(sent[0])
    assert "dispatch@example.com" in msg["From"]
    assert msg["Reply-To"] is None


@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "a b@x.com"])
def test_malformed_recipient_rejected_without_calling_gmail(sent, bad):
    with pytest.raises(gmail_sender.GmailSendError) as exc:
        gmail_sender.send_email(to=bad, subject="s", html="x")
    assert exc.value.status_code == 422
    assert sent == []
    assert gmail_sender.recent()[0]["ok"] is False


def test_gmail_api_failure_is_raised_and_logged(monkeypatch):
    monkeypatch.setattr(gmail_sender.httpx, "post", lambda *a, **k: FakeResponse(403, {"error": {"message": "Forbidden", "errors": [{"reason": "insufficientPermissions"}]}}))
    with pytest.raises(gmail_sender.GmailSendError, match="insufficientPermissions"):
        gmail_sender.send_email(to="a@x.com", subject="s", html="x", purpose="assignment-driver")
    entry = gmail_sender.recent()[0]
    assert entry["ok"] is False and entry["purpose"] == "assignment-driver"


def test_not_configured_raises_503(monkeypatch):
    monkeypatch.delenv("GMAIL_COMMS_REFRESH_TOKEN")
    gmail_sender._token.update(value=None, expires_at=0)
    with pytest.raises(gmail_sender.GmailSendError) as exc:
        gmail_sender.send_email(to="a@x.com", subject="s", html="x")
    assert exc.value.status_code == 503


def test_dedupe_key_blocks_repeat_but_not_after_failure(monkeypatch, sent):
    first = gmail_sender.send_email(to="a@x.com", subject="s", html="x", dedupe_key="k")
    second = gmail_sender.send_email(to="a@x.com", subject="s", html="x", dedupe_key="k")
    other = gmail_sender.send_email(to="a@x.com", subject="s", html="x", dedupe_key="k2")
    assert first["id"] and second.get("duplicate") and other["id"]
    assert len(sent) == 2

    gmail_sender._sent_keys.clear()
    monkeypatch.setattr(gmail_sender.httpx, "post", lambda *a, **k: FakeResponse(500, {"error": "boom"}))
    with pytest.raises(gmail_sender.GmailSendError):
        gmail_sender.send_email(to="a@x.com", subject="s", html="x", dedupe_key="retry")
    assert "retry" not in gmail_sender._sent_keys


def test_dedupe_window_expires(sent):
    gmail_sender.send_email(to="a@x.com", subject="s", html="x", dedupe_key="k")
    gmail_sender._sent_keys["k"] -= gmail_sender.DEDUPE_WINDOW_SECONDS + 1
    gmail_sender.send_email(to="a@x.com", subject="s", html="x", dedupe_key="k")
    assert len(sent) == 2


def test_assignment_emails_send_driver_and_team_once(monkeypatch, sent):
    monkeypatch.setenv("JWT_SECRET", "test")
    from routers import assignment
    from types import SimpleNamespace

    monkeypatch.setattr(assignment.staff_directory_cache, "notify_list", lambda: [{"email": "t1@x.com"}, {"email": "t2@x.com"}])
    driver = SimpleNamespace(name="Juan", email="juan@x.com")
    for _ in range(2):
        assignment._send_assignment_emails_direct(driver, "Driver subj", "<p>d</p>", "Team subj", "<p>t</p>", "NAN1|SO1|d1")
    msgs = [parse(raw) for raw in sent]
    assert [m["To"] for m in msgs] == ["juan@x.com", "t1@x.com, t2@x.com"]
    assert [m["Subject"] for m in msgs] == ["Driver subj", "Team subj"]


def test_no_n8n_email_webhook_is_called(monkeypatch, sent):
    monkeypatch.setenv("JWT_SECRET", "test")
    import inspect
    from routers import assignment

    module = inspect.getsource(assignment)
    assert "intellifleet-logistics-driver-assignment" not in module and "_EMAIL_WEBHOOK" not in module
    assert len(assignment._NOTIFICATION_WEBHOOKS) == 3  # whatsapp, sms, voice only


# ---- Send-As identity ----

def _aliases(monkeypatch, items, status=200):
    monkeypatch.setattr(gmail_sender.httpx, "get", lambda *a, **k: FakeResponse(status, {"sendAs": items}))
    gmail_sender._sendas.update(addresses=None, fetched_at=0.0)
    gmail_sender._labels.update(ids={"Logistics": "L1", "Logistics/Sent": "L2"}, fetched_at=9e12)


PRIMARY = {"sendAsEmail": "dispatch@example.com", "isPrimary": True}
LOGISTICS = {"sendAsEmail": "martin.logistics@rareglobalfood.com", "verificationStatus": "accepted"}


def test_unavailable_from_alias_is_refused_with_clear_error(monkeypatch, sent):
    _aliases(monkeypatch, [PRIMARY])
    monkeypatch.setenv("GMAIL_FROM_ADDRESS", "martin.logistics@rareglobalfood.com")
    with pytest.raises(gmail_sender.GmailSendError, match="not a verified Gmail Send-As alias") as exc:
        gmail_sender.send_email(to="a@x.com", subject="s", html="x")
    assert exc.value.status_code == 503 and sent == []


def test_pending_alias_is_not_available(monkeypatch, sent):
    _aliases(monkeypatch, [PRIMARY, {**LOGISTICS, "verificationStatus": "pending"}])
    monkeypatch.setenv("GMAIL_FROM_ADDRESS", LOGISTICS["sendAsEmail"])
    with pytest.raises(gmail_sender.GmailSendError):
        gmail_sender.send_email(to="a@x.com", subject="s", html="x")


def test_verified_alias_is_used_with_name_and_reply_to(monkeypatch, sent):
    _aliases(monkeypatch, [PRIMARY, LOGISTICS])
    monkeypatch.setenv("GMAIL_FROM_ADDRESS", LOGISTICS["sendAsEmail"])
    monkeypatch.setenv("GMAIL_FROM_NAME", "Martin Cuico")
    monkeypatch.setenv("GMAIL_REPLY_TO", LOGISTICS["sendAsEmail"])
    gmail_sender.send_email(to="a@x.com", subject="s", html="x")
    msg = parse(sent[0])
    assert msg["From"] == "Martin Cuico <martin.logistics@rareglobalfood.com>"
    assert msg["Reply-To"] == "martin.logistics@rareglobalfood.com"


def test_identity_status_reports_alias_availability_without_secrets(monkeypatch):
    _aliases(monkeypatch, [PRIMARY])
    monkeypatch.setenv("GMAIL_FROM_ADDRESS", LOGISTICS["sendAsEmail"])
    status = gmail_sender.identity_status()
    assert status["authenticatedMailbox"] == "dispatch@example.com"
    assert status["sendAsAliasAvailable"] is False and status["usable"] is False and "error" in status
    assert "secret" not in str(status).lower() and "refresh" not in str(status).lower()
    _aliases(monkeypatch, [PRIMARY, LOGISTICS])
    assert gmail_sender.identity_status()["sendAsAliasAvailable"] is True
    monkeypatch.delenv("GMAIL_FROM_ADDRESS")
    assert gmail_sender.identity_status()["effectiveFromAddress"] == "dispatch@example.com"


def test_unknown_alias_state_does_not_block_sending(monkeypatch, sent):
    _aliases(monkeypatch, [], status=403)
    monkeypatch.setenv("GMAIL_FROM_ADDRESS", LOGISTICS["sendAsEmail"])
    gmail_sender.send_email(to="a@x.com", subject="s", html="x")
    assert len(sent) == 1


def test_dedupe_full_lifecycle(monkeypatch, sent):
    kw = dict(to="a@x.com", subject="s", html="x", dedupe_key="evt")
    assert not gmail_sender.send_email(**kw).get("duplicate")          # 1 first send
    assert gmail_sender.send_email(**kw).get("duplicate")              # 2 immediate repeat skipped
    gmail_sender._sent_keys["evt"] -= gmail_sender.DEDUPE_WINDOW_SECONDS + 1
    assert not gmail_sender.send_email(**kw).get("duplicate")          # 3/6 after window: allowed
    gmail_sender._sent_keys.clear()
    monkeypatch.setattr(gmail_sender.httpx, "post", lambda *a, **k: FakeResponse(500, {"error": "x"}))
    with pytest.raises(gmail_sender.GmailSendError):
        gmail_sender.send_email(**kw)                                  # 4 failure not recorded
    monkeypatch.setattr(gmail_sender.httpx, "post", lambda *a, **k: FakeResponse(200, {"id": "ok", "threadId": "t"}))
    assert gmail_sender.send_email(**kw)["id"] == "ok"                 # 5 retry allowed
