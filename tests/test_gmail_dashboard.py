"""The dashboard Gmail view is LOGISTICS-ONLY. A fake mailbox holds logistics, sales and generic
Martin mail together; only the labels `Logistics` / `Logistics/Sent` may ever be listed."""
import asyncio
import base64
from datetime import date, datetime, timezone

import httpx
import pytest

JWT = "test"
LOGISTICS_ID, SENT_ID = "Label_AAA111", "Label_BBB222"  # deliberately NOT derivable from the names
DAY = date(2026, 10, 5)
NOON = int(datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)  # 12:00 Manila
OTHER_DAY = int(datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def message(mid, thread, sender, to, subject, labels, body="hello", at=NOON, date_header="Mon, 05 Oct 2026 12:00:00 +0800"):
    return {"id": mid, "threadId": thread, "labelIds": labels, "internalDate": str(at), "snippet": body[:30],
            "payload": {"mimeType": "text/plain", "headers": [{"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject}, {"name": "Date", "value": date_header}], "body": {"data": _b64(body)}}}


MAILBOX = [
    # logistics conversation, inbound still in INBOX + our reply
    message("m1", "T-driver", "Juan <juan@example.com>", "martin.logistics@rareglobalfood.com", "Re: Driver assignment", [LOGISTICS_ID, "UNREAD", "INBOX"], "Confirmed", NOON),
    message("m2", "T-driver", "Martin Cuico <martin.logistics@rareglobalfood.com>", "juan@example.com", "Re: Driver assignment", [LOGISTICS_ID, "SENT", SENT_ID], "Thanks", NOON + 1),
    # logistics conversation whose inbound message was ARCHIVED (no INBOX label any more)
    message("m3", "T-archived", "Ana <ana@example.com>", "martin.logistics@rareglobalfood.com", "Pickup question", [LOGISTICS_ID], "Where?", NOON),
    message("m4", "T-archived", "Martin Cuico <martin.logistics@rareglobalfood.com>", "ana@example.com", "Re: Pickup question", [LOGISTICS_ID, "SENT", SENT_ID], "Mets", NOON + 1),
    # backend-sent logistics mail with no inbound (team notification)
    message("m5", "T-team", "Martin Cuico <martin.logistics@rareglobalfood.com>", "ops@x.com", "[Driver Reply] Juan - NAN1234", [LOGISTICS_ID, "SENT", SENT_ID], "Status"),
    # NOT logistics: sales + generic Martin mail (must never appear)
    message("s1", "T-sales-in", "Customer <buyer@customer.com>", "martin@rareglobalfood.com", "Pricing question", ["INBOX", "UNREAD"], "price?"),
    message("s2", "T-sales-out", "Martin Reyes <martin@rareglobalfood.com>", "buyer@customer.com", "Quote 2026", ["SENT"], "quote"),
    message("g1", "T-generic-in", "Newsletter <news@vendor.com>", "martin@rareglobalfood.com", "Weekly digest", ["INBOX", "CATEGORY_PROMOTIONS"], "ads"),
    message("g2", "T-generic-out", "Martin <martin@rareglobalfood.com>", "friend@x.com", "Lunch", ["SENT"], "lunch"),
    # logistics but on another day: outside the selected date
    message("o1", "T-old", "Juan <juan@example.com>", "martin.logistics@rareglobalfood.com", "Old thread", [LOGISTICS_ID, "INBOX"], "old", OTHER_DAY),
]
NON_LOGISTICS_THREADS = {"T-sales-in", "T-sales-out", "T-generic-in", "T-generic-out"}


class Gmail:
    """Fake Gmail HTTP layer: records every call; any write (POST) fails the test."""

    def __init__(self, labels=None):
        self.labels = {"INBOX": "INBOX", "SENT": "SENT", "Logistics": LOGISTICS_ID, "Logistics/Sent": SENT_ID} if labels is None else labels
        self.list_calls, self.thread_calls, self.urls, self.posts = [], [], [], []

    def sync_get(self, url, **kw):
        self.urls.append(url)
        assert url.endswith("/labels"), f"unexpected sync GET {url}"
        return httpx.Response(200, json={"labels": [{"id": i, "name": n} for n, i in self.labels.items()]})

    def sync_post(self, url, **kw):
        self.posts.append(url)
        raise AssertionError(f"the dashboard must not write to Gmail: POST {url}")

    def client_class(self):
        gmail = self

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None, params=None):
                gmail.urls.append(url)
                params = params or {}
                if url.endswith("/messages"):
                    gmail.list_calls.append(dict(params))
                    q = params.get("q", "")
                    after = int(q.split("after:")[1].split()[0]) * 1000
                    before = int(q.split("before:")[1].split()[0]) * 1000
                    hits = [m for m in MAILBOX if params["labelIds"] in m["labelIds"] and after <= int(m["internalDate"]) <= before and not ("-in:sent" in q and "SENT" in m["labelIds"])]
                    return httpx.Response(200, json={"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in hits], "resultSizeEstimate": len(hits)})
                if "/threads/" in url:
                    thread_id = url.rsplit("/", 1)[1]
                    gmail.thread_calls.append(thread_id)
                    return httpx.Response(200, json={"id": thread_id, "messages": [m for m in MAILBOX if m["threadId"] == thread_id]})
                raise AssertionError(f"unexpected GET {url}")

            async def post(self, *a, **k):
                gmail.posts.append(a[0] if a else "?")
                raise AssertionError("the dashboard must not write to Gmail")

        return FakeClient


@pytest.fixture
def view(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", JWT)
    from routers import gmail as router
    from services import gmail_sender

    gmail_sender._label_lookup.update(labels=None, fetched_at=0.0)
    gmail_sender._token.update(value="tok", expires_at=9e12)
    monkeypatch.setenv("GMAIL_COMMS_CLIENT_ID", "i")
    monkeypatch.setenv("GMAIL_COMMS_CLIENT_SECRET", "s")
    monkeypatch.setenv("GMAIL_COMMS_REFRESH_TOKEN", "r")

    def install(labels=None):
        fake = Gmail(labels)
        monkeypatch.setattr(httpx, "get", fake.sync_get)
        monkeypatch.setattr(httpx, "post", fake.sync_post)
        monkeypatch.setattr(router.httpx, "AsyncClient", fake.client_class())
        monkeypatch.setattr(router, "_get_access_token", lambda: asyncio.sleep(0, "tok"))
        gmail_sender._label_lookup.update(labels=None, fetched_at=0.0)
        return fake

    def call(folder="inbox"):
        return asyncio.run(router.list_today_messages(selected_date=DAY, folder=folder))

    return SimpleView(install, call, router)


class SimpleView:
    def __init__(self, install, call, router):
        self.install, self.call, self.router = install, call, router


def ids(result):
    return {t["thread_id"] for t in result["threads"]}


def all_message_ids(result):
    return {m["id"] for t in result["threads"] for m in t["messages"]}


# ---- 1, 2: each folder shows only its label ----

def test_inbox_is_logistics_inbound_only(view):
    view.install()
    result = view.call("inbox")
    assert result["scope"] == "logistics" and result["folder"] == "inbox"
    assert ids(result) == {"T-driver", "T-archived"}  # not T-team (sent-only), not T-old (other day)


def test_sent_is_logistics_sent_only(view):
    view.install()
    result = view.call("sent")
    assert ids(result) == {"T-driver", "T-archived", "T-team"}
    assert any(m["is_sent"] for t in result["threads"] for m in t["messages"])


def test_default_folder_is_inbox(view):
    view.install()
    assert ids(asyncio.run(view.router.list_today_messages(selected_date=DAY))) == {"T-driver", "T-archived"}


# ---- 3-6: nothing outside the logistics labels, in either folder ----

@pytest.mark.parametrize("folder", ["inbox", "sent"])
def test_non_logistics_mail_is_never_returned(view, folder):
    view.install()
    result = view.call(folder)
    assert not ids(result) & NON_LOGISTICS_THREADS  # sales, generic Martin inbox, generic Martin sent
    assert not all_message_ids(result) & {"s1", "s2", "g1", "g2"}
    assert "Pricing question" not in {t["subject"] for t in result["threads"]} and "Quote 2026" not in {t["subject"] for t in result["threads"]}


@pytest.mark.parametrize("folder", ["inbox", "sent"])
def test_the_mailbox_is_never_listed_without_a_logistics_label(view, folder):
    fake = view.install()
    view.call(folder)
    assert fake.list_calls and all(call["labelIds"] in {LOGISTICS_ID, SENT_ID} for call in fake.list_calls)
    assert not set(fake.thread_calls) & NON_LOGISTICS_THREADS  # their threads are not even fetched


# ---- 7, 8: complete conversations ----

def test_conversation_stays_visible_after_the_inbound_message_is_archived(view):
    view.install()
    result = view.call("inbox")
    archived = next(t for t in result["threads"] if t["thread_id"] == "T-archived")
    assert [m["id"] for m in archived["messages"]] == ["m3", "m4"]  # inbound (no INBOX label) + our reply
    inbound = archived["messages"][0]
    assert inbound["is_sent"] is False


def test_backend_replies_remain_visible_in_logistics_sent(view):
    view.install()
    sent = view.call("sent")
    driver = next(t for t in sent["threads"] if t["thread_id"] == "T-driver")
    assert [m["id"] for m in driver["messages"]] == ["m1", "m2"]  # whole conversation, reply included
    assert {m["id"] for m in driver["messages"] if m["is_sent"]} == {"m2"}


# ---- 9, 10: read-only, no n8n ----

@pytest.mark.parametrize("folder", ["inbox", "sent"])
def test_no_write_no_send_no_label_creation_and_no_n8n(view, folder):
    fake = view.install()
    view.call(folder)
    assert fake.posts == []  # no send, no modify (archive), no label create
    assert all(url.startswith("https://gmail.googleapis.com/") for url in fake.urls)
    assert not any("n8n" in url or "/webhook/" in url or url.endswith("/send") or "/modify" in url for url in fake.urls)


# ---- query construction / label handling ----

def test_queries_use_looked_up_label_ids_and_day_bounds(view):
    fake = view.install()
    view.call("inbox")
    view.call("sent")
    inbox, sent = fake.list_calls
    assert inbox["labelIds"] == LOGISTICS_ID and "-in:sent" in inbox["q"] and "after:" in inbox["q"] and "before:" in inbox["q"]
    assert sent["labelIds"] == SENT_ID and "-in:sent" not in sent["q"] and "after:" in sent["q"]
    import inspect
    source = inspect.getsource(view.router)
    assert "Label_" not in source  # ids are looked up by name, never hard-coded


def test_missing_label_shows_nothing_and_never_creates_it(view):
    fake = view.install(labels={"INBOX": "INBOX", "SENT": "SENT"})
    result = view.call("inbox")
    assert result["threads"] == [] and result["missingLabels"] == ["Logistics"]
    assert fake.list_calls == [] and fake.posts == []
    result = view.call("sent")
    assert result["threads"] == [] and result["missingLabels"] == ["Logistics/Sent"]


def test_lookup_label_ids_is_read_only_and_case_insensitive(view):
    from services import gmail_sender

    fake = view.install(labels={"logistics": "X1", "LOGISTICS/SENT": "X2", "Other": "X3"})
    assert gmail_sender.lookup_label_ids(["Logistics", "Logistics/Sent", "Missing"]) == {"Logistics": "X1", "Logistics/Sent": "X2"}
    assert fake.posts == []
    gmail_sender.lookup_label_ids(["Logistics"])  # cached: one labels.list within the TTL
    assert sum(url.endswith("/labels") for url in fake.urls) == 1


def test_frontend_no_longer_uses_the_whole_mailbox_call():
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "artifacts" / "intellifleet" / "src"
    api = (src / "services" / "api" / "gmail.ts").read_text(encoding="utf-8")
    panel = (src / "components" / "dispatch" / "CommsGateway.tsx").read_text(encoding="utf-8")
    assert "listTodayThreads" not in api + panel and "listLogisticsThreads" in api and "folder" in api
    assert "Logistics Inbox" in panel and "Logistics Sent" in panel
    assert "m.is_sent" not in panel  # no client-side inbox/sent splitting of a whole-mailbox response


def test_conversation_is_ordered_by_receive_time_not_by_date_header_text(view, monkeypatch):
    """The driver writes from +08:00, our reply is stamped -07:00: as ISO strings the reply sorts first,
    but it was received 1 second later. The view must show the driver's message first."""
    import sys

    extra = [
        message("x1", "T-order", "Juan <juan@example.com>", "martin.logistics@rareglobalfood.com", "Order check", [LOGISTICS_ID, "INBOX"], "hi", NOON, "Mon, 05 Oct 2026 12:00:00 +0800"),
        message("x2", "T-order", "Martin Cuico <martin.logistics@rareglobalfood.com>", "juan@example.com", "Re: Order check", [LOGISTICS_ID, "SENT", SENT_ID], "reply", NOON + 1000, "Sun, 04 Oct 2026 21:00:01 -0700"),
    ]
    monkeypatch.setattr(sys.modules[__name__], "MAILBOX", MAILBOX + extra)
    view.install()
    thread = next(t for t in view.call("inbox")["threads"] if t["thread_id"] == "T-order")
    assert [m["id"] for m in thread["messages"]] == ["x1", "x2"]
    assert [m["is_sent"] for m in thread["messages"]] == [False, True]
