"""twilio_logs: filtering, pagination, caching and error handling against a faked Twilio API."""
import os
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("JWT_SECRET", "test-only-secret")

from services import twilio_logs

SENDER = "whatsapp:+639171145694"
TEMPLATE = "Hi Jed, you've been assigned truck ABC123 for today's deliveries."


def msg(sid, direction, body, to=SENDER, frm="whatsapp:+63900", status="delivered", **extra):
    return {"sid": sid, "direction": direction, "body": body, "to": to, "from": frm, "status": status,
            "date_created": "2026-10-01T06:38:00+00:00", "date_sent": "2026-10-01T06:38:02+00:00",
            "error_code": None, "error_message": None, "price": None, "price_unit": None, **extra}


class FakeTwilio:
    def __init__(self, pages_out, inbound):
        self.pages_out, self.inbound, self.requests = pages_out, inbound, []

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        q = dict(request.url.params)
        if "page" in q:  # follow-up page reached through next_page_uri
            return httpx.Response(200, json=self.pages_out[1])
        if q.get("From") == SENDER:
            return httpx.Response(200, json=self.pages_out[0])
        if q.get("To") == SENDER:
            return httpx.Response(200, json={"messages": self.inbound, "next_page_uri": None})
        return httpx.Response(400, json={})


class TwilioLogsTests(unittest.TestCase):
    def setUp(self):
        twilio_logs._cache.clear()
        env = {"TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_API_KEY_SID": "SKtest", "TWILIO_API_KEY_SECRET": "secret", "TWILIO_WHATSAPP_FROM": SENDER}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, fake):
        real = httpx.Client
        patcher = patch.object(twilio_logs.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(fake.handler), **kw))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self):
        page1 = {"messages": [msg("MM1", "outbound-api", TEMPLATE, to="whatsapp:+639171111111"),
                              msg("MMsales", "outbound-api", "Hi Ana, thanks for your interest in our offer", to="whatsapp:+639172222222"),
                              msg("MMreply", "outbound-reply", TEMPLATE, to="whatsapp:+639173333333")],
                 "next_page_uri": "/2010-04-01/Accounts/ACtest/Messages.json?page=1&PageSize=200&From=x"}
        page2 = {"messages": [msg("MM2", "outbound-api", "Hi Pau, you’ve been assigned truck XYZ789.", to="whatsapp:+639174444444", status="failed", error_code=63016, error_message="outside window")],
                 "next_page_uri": None}
        inbound = [msg("IN1", "inbound", "Confirmed", to=SENDER, frm="whatsapp:+639171111111"),
                   msg("IN2", "inbound", "stranger", to=SENDER, frm="whatsapp:+639179999999")]
        return FakeTwilio([page1, page2], inbound)

    def test_template_filter_pagination_and_inbound_matching(self):
        fake = self.fake()
        self.install(fake)
        result = twilio_logs.get_logs("2026-09-23")
        self.assertEqual([r["sid"] for r in result["outbound"]], ["MM1", "MM2"])  # sales + non-api excluded; curly apostrophe ok
        self.assertEqual(result["outbound"][1]["error_code"], 63016)
        self.assertEqual([r["sid"] for r in result["inbound"]], ["IN1"])  # only replies from template recipients
        self.assertEqual(result["inbound"][0]["from"], "+639171111111")
        self.assertEqual(result["outbound"][0]["to"], "+639171111111")
        first = fake.requests[0].url.params
        self.assertEqual((first["DateSent>"], first["PageSize"], first["From"]), ("2026-09-23", "200", SENDER))

    def test_cache_60s_and_failures_not_cached(self):
        fake = self.fake()
        self.install(fake)
        twilio_logs.get_logs("2026-09-23")
        calls = len(fake.requests)
        self.assertTrue(twilio_logs.get_logs("2026-09-23")["cached"])
        self.assertEqual(len(fake.requests), calls)  # second call served from memory
        twilio_logs._cache["2026-09-23"] = (twilio_logs._cache["2026-09-23"][0] - 61, twilio_logs._cache["2026-09-23"][1])
        self.assertFalse(twilio_logs.get_logs("2026-09-23")["cached"])  # expired

    def test_errors_and_missing_config(self):
        bad = FakeTwilio([{}, {}], [])
        bad.handler = lambda request: httpx.Response(401, json={"message": "nope"})
        self.install(bad)
        with self.assertRaises(twilio_logs.TwilioError):
            twilio_logs.get_logs("2026-09-24")
        self.assertNotIn("2026-09-24", twilio_logs._cache)
        with patch.dict(os.environ, {"TWILIO_API_KEY_SID": ""}):
            with self.assertRaises(twilio_logs.TwilioNotConfigured):
                twilio_logs.get_logs("2026-09-25")

    def test_dates_are_iso_utc_and_sorted_chronologically(self):
        page1 = {"messages": [msg("NEW", "outbound-api", TEMPLATE, to="whatsapp:+639171111111", date_sent="Thu, 01 Oct 2026 06:38:14 +0000", date_created="Thu, 01 Oct 2026 06:38:13 +0000"),
                              msg("OLD", "outbound-api", TEMPLATE, to="whatsapp:+639172222222", date_sent="Wed, 23 Sep 2026 08:58:53 +0000", date_created="Wed, 23 Sep 2026 08:58:52 +0000")],
                 "next_page_uri": None}
        self.install(FakeTwilio([page1, {}], []))
        result = twilio_logs.get_logs("2026-09-26")
        self.assertEqual([r["sid"] for r in result["outbound"]], ["OLD", "NEW"])
        self.assertEqual(result["outbound"][1]["date_sent"], "2026-10-01T06:38:14+00:00")

    def test_is_logistics_template(self):
        self.assertTrue(twilio_logs.is_logistics_template(msg("a", "outbound-api", TEMPLATE)))
        self.assertFalse(twilio_logs.is_logistics_template(msg("a", "outbound-api", "Hello Jed, you've been assigned truck 1")))
        self.assertFalse(twilio_logs.is_logistics_template(msg("a", "inbound", TEMPLATE)))

    def test_endpoint_status_codes(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import communications
        app = FastAPI()
        app.include_router(communications.router)
        app.dependency_overrides = {}
        from auth import dependencies
        for dep in communications.router.dependencies:
            app.dependency_overrides[dep.dependency] = lambda: None
        client = TestClient(app)
        self.assertEqual(client.get("/api/communications/whatsapp/logs?since=bad").status_code, 422)
        with patch.dict(os.environ, {"TWILIO_API_KEY_SID": ""}):
            self.assertEqual(client.get("/api/communications/whatsapp/logs?since=2026-09-23").status_code, 503)
        self.install(self.fake())
        body = client.get("/api/communications/whatsapp/logs?since=2026-09-23").json()
        self.assertEqual(len(body["outbound"]), 2)


if __name__ == "__main__":
    unittest.main()
