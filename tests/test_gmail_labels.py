"""Logistics / Logistics/Sent labeling after a successful send. Gmail is mocked at the HTTP layer."""
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


class FakeGmail:
    """Routes the Gmail endpoints the sender touches; records every call."""

    def __init__(self, labels=None, send_status=200, modify_status=200, list_status=200, create_status=200):
        self.labels = {"INBOX": "INBOX", "Logistics": "Label_1", "Logistics/Sent": "Label_2"} if labels is None else labels
        self.send_status, self.modify_status, self.list_status, self.create_status = send_status, modify_status, list_status, create_status
        self.sends, self.modifies, self.creates, self.other_posts = [], [], [], []

    def get(self, url, **kw):
        if url == gmail_sender.LABELS_URL:
            return FakeResponse(self.list_status, {"labels": [{"id": i, "name": n} for n, i in self.labels.items()]})
        return FakeResponse(200, {"sendAs": []})

    def post(self, url, **kw):
        if url == gmail_sender.SEND_URL:
            self.sends.append(kw["json"]["raw"])
            if self.send_status != 200:
                return FakeResponse(self.send_status, {"error": "send failed"})
            return FakeResponse(200, {"id": "MSG1", "threadId": "THR1"})
        if url == gmail_sender.LABELS_URL:
            self.creates.append(kw["json"]["name"])
            return FakeResponse(self.create_status, {"id": f"New_{len(self.creates)}"})
        if url.startswith("https://gmail.googleapis.com/gmail/v1/users/me/messages/") and url.endswith("/modify"):
            self.modifies.append(kw["json"])
            return FakeResponse(self.modify_status, {"id": url.rsplit("/", 2)[-2]} if self.modify_status == 200 else {"error": "denied"})
        self.other_posts.append(url)
        return FakeResponse(200, {})


@pytest.fixture(autouse=True)
def env(monkeypatch):
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
    gmail_sender._labels.update(ids=None, fetched_at=0.0)


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs):
        gm = FakeGmail(**kwargs)
        monkeypatch.setattr(gmail_sender.httpx, "get", gm.get)
        monkeypatch.setattr(gmail_sender.httpx, "post", gm.post)
        return gm

    return install


def test_labels_applied_to_sent_message_by_looked_up_ids(fake):
    gm = fake()
    result = gmail_sender.send_email(to="a@x.com", subject="s", html="x", purpose="assignment-driver")
    assert result["id"] == "MSG1"
    assert gm.modifies == [{"addLabelIds": ["Label_1", "Label_2"]}]
    assert gm.creates == []
    assert result["labels"] == {"logistics": "applied", "logisticsSent": "applied"}
    entry = gmail_sender.recent()[0]
    assert entry["ok"] and entry["messageId"] == "MSG1" and entry["labels"]["logisticsSent"] == "applied"


def test_missing_label_is_created_once_then_applied(fake):
    gm = fake(labels={"INBOX": "INBOX", "Logistics": "Label_1"})
    result = gmail_sender.send_email(to="a@x.com", subject="s", html="x")
    assert gm.creates == ["Logistics/Sent"]
    assert gm.modifies == [{"addLabelIds": ["Label_1", "New_1"]}]
    assert result["labels"]["logisticsSent"] == "applied"
    gmail_sender._sent_keys.clear()
    gmail_sender.send_email(to="a@x.com", subject="s2", html="x")
    assert gm.creates == ["Logistics/Sent"]  # ids cached, nothing created again


def test_label_name_match_is_case_insensitive_no_duplicate(fake):
    gm = fake(labels={"logistics": "Label_1", "LOGISTICS/SENT": "Label_2"})
    gmail_sender.send_email(to="a@x.com", subject="s", html="x")
    assert gm.creates == [] and gm.modifies == [{"addLabelIds": ["Label_1", "Label_2"]}]


def test_label_failure_does_not_fail_or_resend_email(fake):
    gm = fake(modify_status=403)
    result = gmail_sender.send_email(to="a@x.com", subject="s", html="x", dedupe_key="evt")
    assert result["id"] == "MSG1" and len(gm.sends) == 1
    assert result["labels"]["logistics"] == "failed" and "403" in result["labels"]["error"]
    entry = gmail_sender.recent()[0]
    assert entry["ok"] is True and entry["labels"]["logisticsSent"] == "failed"
    assert gmail_sender.send_email(to="a@x.com", subject="s", html="x", dedupe_key="evt").get("duplicate")
    assert len(gm.sends) == 1


def test_label_lookup_and_create_failures_are_non_fatal(fake):
    fake(list_status=500)
    assert gmail_sender.send_email(to="a@x.com", subject="s", html="x")["labels"]["logistics"] == "failed"
    gm = fake(labels={"INBOX": "INBOX"}, create_status=403)
    gmail_sender._sent_keys.clear()
    result = gmail_sender.send_email(to="a@x.com", subject="s2", html="x")
    assert result["id"] == "MSG1" and result["labels"]["logistics"] == "failed" and gm.modifies == []


def test_one_label_missing_still_applies_the_other(fake):
    gm = fake(labels={"INBOX": "INBOX", "Logistics": "Label_1"}, create_status=403)
    result = gmail_sender.send_email(to="a@x.com", subject="s", html="x")
    assert gm.modifies == [{"addLabelIds": ["Label_1"]}]
    assert result["labels"]["logistics"] == "applied" and result["labels"]["logisticsSent"] == "failed"


def test_send_failure_never_attempts_labeling(fake):
    gm = fake(send_status=500)
    with pytest.raises(gmail_sender.GmailSendError):
        gmail_sender.send_email(to="a@x.com", subject="s", html="x")
    assert gm.modifies == [] and gm.creates == []


def test_duplicate_skip_does_not_label_again(fake):
    gm = fake()
    gmail_sender.send_email(to="a@x.com", subject="s", html="x", dedupe_key="k")
    gmail_sender.send_email(to="a@x.com", subject="s", html="x", dedupe_key="k")
    assert len(gm.sends) == 1 and len(gm.modifies) == 1


def test_only_gmail_is_called_and_identity_is_unchanged(monkeypatch, fake):
    monkeypatch.setenv("GMAIL_FROM_ADDRESS", "martin.logistics@rareglobalfood.com")
    monkeypatch.setenv("GMAIL_FROM_NAME", "Martin Cuico")
    monkeypatch.setenv("GMAIL_REPLY_TO", "martin.logistics@rareglobalfood.com")
    gm = fake()
    alias_list = {"sendAs": [{"sendAsEmail": "dispatch@example.com", "isPrimary": True}, {"sendAsEmail": "martin.logistics@rareglobalfood.com", "verificationStatus": "accepted"}]}
    monkeypatch.setattr(gmail_sender.httpx, "get", lambda url, **kw: FakeResponse(200, alias_list) if url == gmail_sender.SENDAS_URL else gm.get(url, **kw))
    gmail_sender.send_email(to="a@x.com", subject="s", html="x")
    msg = email.message_from_bytes(base64.urlsafe_b64decode(gm.sends[0]), policy=policy.default)
    assert msg["From"] == "Martin Cuico <martin.logistics@rareglobalfood.com>"
    assert msg["Reply-To"] == "martin.logistics@rareglobalfood.com"
    assert gm.other_posts == []  # nothing but Gmail endpoints (no n8n)


def test_label_logistics_can_be_disabled(fake):
    gm = fake()
    assert gmail_sender.send_email(to="a@x.com", subject="s", html="x", label_logistics=False)["labels"] is None
    assert gm.modifies == []


# ---- reply into an existing thread: Logistics on the thread, Logistics/Sent on the message ----

def test_thread_reply_labels_thread_and_sent_message_and_survives_partial_failure(monkeypatch):
    posts = []

    def post(url, **kw):
        posts.append((url, kw["json"]))
        if url == gmail_sender.SEND_URL:
            return FakeResponse(200, {"id": "R1", "threadId": "T9"})
        if url == gmail_sender.THREAD_MODIFY_URL.format(id="T9"):
            return FakeResponse(403, {"error": "denied"})
        return FakeResponse(200, {"id": "R1"})

    monkeypatch.setattr(gmail_sender.httpx, "post", post)
    monkeypatch.setattr(gmail_sender.httpx, "get", FakeGmail().get)
    result = gmail_sender.send_email(to="a@x.com", subject="Re: s", html="x", thread_id="T9", in_reply_to="<a@b>")
    assert [p[0] for p in posts if p[0] != gmail_sender.SEND_URL] == [gmail_sender.THREAD_MODIFY_URL.format(id="T9"), gmail_sender.MODIFY_URL.format(id="R1")]
    assert result["id"] == "R1" and sum(p[0] == gmail_sender.SEND_URL for p in posts) == 1  # never resent
    assert result["labels"]["logistics"] == "failed" and result["labels"]["logisticsSent"] == "applied" and "403" in result["labels"]["error"]


def test_custom_header_reaches_the_mime(fake):
    gm = fake()
    gmail_sender.send_email(to="a@x.com", subject="s", html="x", extra_headers={"X-Logistics-Agent-Action": "ACK_CONFIRM"})
    msg = email.message_from_bytes(base64.urlsafe_b64decode(gm.sends[0]), policy=policy.default)
    assert msg["X-Logistics-Agent-Action"] == "ACK_CONFIRM"


def test_remove_inbox_label_is_message_level_and_idempotent(fake):
    gm = fake()
    assert gmail_sender.remove_inbox_label("IN1", ["INBOX", "Label_1"]) == {"inbox": "removed"}
    assert gm.modifies == [{"removeLabelIds": ["INBOX"]}]

    assert gmail_sender.remove_inbox_label("IN2", ["Label_1"]) == {"inbox": "skipped", "reason": "INBOX already absent"}
    assert gm.modifies == [{"removeLabelIds": ["INBOX"]}]
