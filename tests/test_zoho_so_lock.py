import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "test")
os.environ.setdefault("ZOHO_ORG_ID", "org-test")
os.environ.setdefault("ZOHO_SO_LOCK_API_BASE", "https://www.zohoapis.com/inventory/v1")
os.environ.setdefault("ZOHO_SO_LOCK_CONFIGURATION_ID", "4489499000111461258")
os.environ.setdefault("ZOHO_LOCK_CLIENT_ID", "lock-client")
os.environ.setdefault("ZOHO_LOCK_CLIENT_SECRET", "lock-secret")
os.environ.setdefault("ZOHO_LOCK_REFRESH_TOKEN", "lock-refresh")

from auth.dependencies import CurrentUser
from routers import load_planning
from services import zoho_client, zoho_so_lock


class FakeResponse(SimpleNamespace):
    def json(self):
        return self.body


def response(status=200, body=None):
    return FakeResponse(status_code=status, headers={}, body=body if body is not None else {"code": 0})


def so_payload(locked=False, config_id="4489499000111461258"):
    return {
        "code": 0,
        "salesorder": {
            "salesorder_id": "SO-ID",
            "lock_details": {
                "is_locked": locked,
                "locking_config_id": config_id,
                "locking_config_name": "For Fulfillment",
                "locked_by": "User",
                "lock_time": "05 Oct 2026 09:22 AM",
                "lock_reason": "Acknowledged",
            },
        },
    }


class ZohoSoLockServiceTests(unittest.TestCase):
    def setUp(self):
        zoho_so_lock.reset_token_cache()
        env = {
            "ZOHO_ORG_ID": "org-test",
            "ZOHO_SO_LOCK_API_BASE": "https://www.zohoapis.com/inventory/v1",
            "ZOHO_SO_LOCK_CONFIGURATION_ID": "4489499000111461258",
            "ZOHO_LOCK_CLIENT_ID": "lock-client",
            "ZOHO_LOCK_CLIENT_SECRET": "lock-secret",
            "ZOHO_LOCK_REFRESH_TOKEN": "lock-refresh",
        }
        self.env = patch.dict(os.environ, env, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(zoho_so_lock.reset_token_cache)

    def test_lock_post_uses_exact_url_form_body_and_lock_token(self):
        calls = []
        bodies = [so_payload(False), {"code": 0}, so_payload(True)]

        def fake_request(method, url, headers=None, params=None, data=None, json=None, timeout=None):
            calls.append({"method": method, "url": url, "headers": headers, "params": params, "data": data, "json": json})
            return response(body=bodies[len(calls) - 1])

        with patch.object(zoho_so_lock, "get_access_token", return_value="LOCK-TOKEN"), patch.object(zoho_so_lock.httpx, "request", fake_request):
            result = zoho_so_lock.lock_salesorder("123", "Pau")

        self.assertTrue(result["locked"])
        post = calls[1]
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["url"], "https://www.zohoapis.com/inventory/v1/lock/4489499000111461258?entity=salesorder&entity_ids=123&organization_id=org-test")
        self.assertIsNone(post["json"])
        self.assertEqual(set(post["data"]), {"JSONString"})
        reason = json.loads(post["data"]["JSONString"])["reason"]
        self.assertIn("Acknowledged by Supply Chain Department via IntelliFleet", reason)
        self.assertIn("Pau", reason)
        self.assertNotIn("Asia/Manila", reason)
        self.assertEqual(post["headers"]["Authorization"], "Zoho-oauthtoken LOCK-TOKEN")

    def test_already_locked_skips_post(self):
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url))
            return response(body=so_payload(True))

        with patch.object(zoho_so_lock, "get_access_token", return_value="LOCK-TOKEN"), patch.object(zoho_so_lock.httpx, "request", fake_request):
            result = zoho_so_lock.lock_salesorder("123", "Pau")

        self.assertTrue(result["locked"])
        self.assertTrue(result["already_locked"])
        self.assertEqual([method for method, _ in calls], ["GET"])

    def test_post_ok_but_verify_unlocked_returns_error(self):
        bodies = [so_payload(False), {"code": 0}, so_payload(False)]

        def fake_request(method, url, **kwargs):
            return response(body=bodies.pop(0))

        with patch.object(zoho_so_lock, "get_access_token", return_value="LOCK-TOKEN"), patch.object(zoho_so_lock.httpx, "request", fake_request):
            result = zoho_so_lock.lock_salesorder("123", "Pau")

        self.assertFalse(result["locked"])
        self.assertEqual(result["lock_error"], "lock_verify_failed")

    def test_scope_error_is_normalized(self):
        bodies = [so_payload(False), {"code": 57, "message": "not authorized scope"}]

        def fake_request(method, url, **kwargs):
            status = 403 if method == "POST" else 200
            return response(status=status, body=bodies.pop(0))

        with patch.object(zoho_so_lock, "get_access_token", return_value="LOCK-TOKEN"), patch.object(zoho_so_lock.httpx, "request", fake_request):
            result = zoho_so_lock.lock_salesorder("123", "Pau")

        self.assertFalse(result["locked"])
        self.assertEqual(result["lock_error"], "lock_credential_lacks_scope")

    def test_missing_env_is_not_configured(self):
        with patch.dict(os.environ, {"ZOHO_LOCK_CLIENT_ID": ""}, clear=False):
            result = zoho_so_lock.lock_salesorder("123", "Pau")
        self.assertFalse(result["locked"])
        self.assertEqual(result["lock_error"], "not_configured")

    def test_lock_token_refresh_is_independent_of_main_client(self):
        zoho_so_lock.reset_token_cache()
        zoho_client._access_token = "MAIN"
        posts = []

        def fake_post(url, data=None, timeout=None):
            posts.append(data)
            return response(body={"access_token": "LOCK-ACCESS", "expires_in": 3600})

        with patch.object(zoho_so_lock.httpx, "post", fake_post):
            self.assertEqual(zoho_so_lock.get_access_token(), "LOCK-ACCESS")
            self.assertEqual(zoho_so_lock.get_access_token(), "LOCK-ACCESS")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["client_id"], "lock-client")
        self.assertEqual(zoho_client._access_token, "MAIN")

    def test_logs_do_not_contain_credentials(self):
        with patch.dict(os.environ, {"ZOHO_LOCK_CLIENT_ID": ""}, clear=False):
            with self.assertLogs("zoho", level="WARNING") as logs:
                zoho_so_lock.log_configuration_warning()
        text = "\n".join(logs.output)
        self.assertIn("not_configured", text)
        self.assertNotIn("lock-secret", text)
        self.assertNotIn("lock-refresh", text)


class AcknowledgeLockRouterTests(unittest.TestCase):
    def setUp(self):
        self.user = CurrentUser(id=1, email="pau@example.com", role="dispatcher", full_name="Pau", status="active")

    def test_acknowledge_success_locks(self):
        cached = SimpleNamespace(order_status="confirmed", salesorder_number="SO-1", raw_json={})
        with patch.object(load_planning.live_sales_order_cache, "find_cached", return_value=cached), \
                patch.object(load_planning, "acknowledge_sales_order", return_value={"code": 0}) as ack, \
                patch.object(load_planning, "lock_salesorder", return_value={"locked": True, "already_locked": False, "lock_status": {}, "lock_error": None}) as lock, \
                patch.object(load_planning.live_sales_order_cache, "mark_acknowledged"), \
                patch.object(load_planning, "_set_acknowledged"):
            body = load_planning.acknowledge_sales_order_route("1", self.user)
        ack.assert_called_once()
        lock.assert_called_once_with("1", user="Pau")
        self.assertTrue(body["acknowledged"])
        self.assertTrue(body["locked"])

    def test_acknowledge_failure_never_locks(self):
        cached = SimpleNamespace(order_status="confirmed", salesorder_number="SO-1", raw_json={})
        with patch.object(load_planning.live_sales_order_cache, "find_cached", return_value=cached), \
                patch.object(load_planning, "acknowledge_sales_order", side_effect=zoho_client.ZohoError("ack failed")), \
                patch.object(load_planning, "lock_salesorder") as lock:
            with self.assertRaises(Exception):
                load_planning.acknowledge_sales_order_route("1", self.user)
        lock.assert_not_called()

    def test_acknowledge_requires_confirmed_parent_status(self):
        cached = SimpleNamespace(order_status="partially shipped", salesorder_number="SO-1", raw_json={})
        with patch.object(load_planning.live_sales_order_cache, "find_cached", return_value=cached), \
                patch.object(load_planning, "acknowledge_sales_order") as ack, \
                patch.object(load_planning, "lock_salesorder") as lock:
            with self.assertRaises(Exception) as caught:
                load_planning.acknowledge_sales_order_route("1", self.user)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("only allows the Acknowledged sub-status on Confirmed", caught.exception.detail)
        self.assertIn("partially shipped", caught.exception.detail)
        ack.assert_not_called()
        lock.assert_not_called()

    def test_zoho_parent_status_error_returns_actionable_conflict(self):
        cached = SimpleNamespace(order_status="confirmed", salesorder_number="SO-1", raw_json={})
        with patch.object(load_planning.live_sales_order_cache, "find_cached", return_value=cached), \
                patch.object(load_planning, "acknowledge_sales_order", side_effect=zoho_client.ZohoError("Zoho Inventory returned HTTP 400: Parent and Entity status differs..")), \
                patch.object(load_planning, "lock_salesorder") as lock:
            with self.assertRaises(Exception) as caught:
                load_planning.acknowledge_sales_order_route("1", self.user)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("only allows the Acknowledged sub-status on Confirmed", caught.exception.detail)
        lock.assert_not_called()

    def test_lock_failure_keeps_acknowledged_and_surfaces_error(self):
        cached = SimpleNamespace(order_status="confirmed", salesorder_number="SO-1", raw_json={})
        with patch.object(load_planning.live_sales_order_cache, "find_cached", return_value=cached), \
                patch.object(load_planning, "acknowledge_sales_order", return_value={"code": 0}), \
                patch.object(load_planning, "lock_salesorder", return_value={"locked": False, "already_locked": False, "lock_status": {}, "lock_error": "boom"}), \
                patch.object(load_planning.live_sales_order_cache, "mark_acknowledged"), \
                patch.object(load_planning, "_set_acknowledged"):
            body = load_planning.acknowledge_sales_order_route("1", self.user)
        self.assertTrue(body["acknowledged"])
        self.assertFalse(body["locked"])
        self.assertEqual(body["lock_error"], "boom")

    def test_bulk_mixed_results(self):
        rows = [
            SimpleNamespace(id="1", order_status="confirmed", salesorder_number="SO-1", raw_json={}),
            SimpleNamespace(id="2", order_status="confirmed", salesorder_number="SO-2", raw_json={}),
        ]
        lock_results = [
            {"locked": True, "already_locked": False, "lock_status": {}, "lock_error": None},
            {"locked": False, "already_locked": False, "lock_status": {}, "lock_error": "boom"},
        ]
        with patch.object(load_planning, "_filtered_rows", return_value=rows), \
                patch.object(load_planning, "acknowledge_sales_order", return_value={"code": 0}), \
                patch.object(load_planning, "lock_salesorder", side_effect=lock_results), \
                patch.object(load_planning.live_sales_order_cache, "mark_acknowledged"), \
                patch.object(load_planning, "_set_acknowledged"):
            body = load_planning.acknowledge_filtered_sales_orders(db=SimpleNamespace(), current_user=self.user)
        self.assertEqual(body["acknowledged_count"], 2)
        self.assertEqual([item["locked"] for item in body["results"]], [True, False])
        self.assertEqual(body["results"][1]["lock_error"], "boom")


# =============================================================================================
# Additional coverage (spec checklist). HTTP is mocked everywhere: no live Zoho call.
# =============================================================================================
import logging
import re

import httpx
from fastapi import HTTPException

from services import zoho_rate_limiter

LOCK_ENV = {
    "ZOHO_ORG_ID": "org-test",
    "ZOHO_SO_LOCK_API_BASE": "https://www.zohoapis.com/inventory/v1",
    "ZOHO_SO_LOCK_CONFIGURATION_ID": "4489499000111461258",
    "ZOHO_LOCK_CLIENT_ID": "lock-client",
    "ZOHO_LOCK_CLIENT_SECRET": "lock-secret-VALUE",
    "ZOHO_LOCK_REFRESH_TOKEN": "lock-refresh-VALUE",
}
CONFIG_ID = "4489499000111461258"


def lock_case_setup(case):
    zoho_so_lock.reset_token_cache()
    zoho_rate_limiter.reset()
    env = patch.dict(os.environ, LOCK_ENV, clear=False)
    env.start()
    case.addCleanup(env.stop)
    case.addCleanup(zoho_so_lock.reset_token_cache)


class Recorder:
    """httpx.request stand-in: scripted responses, every call recorded."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers=None, params=None, data=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "params": params, "data": data, "json": json})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def methods(self):
        return [c["method"] for c in self.calls]


class LockRequestShapeTests(unittest.TestCase):
    def setUp(self):
        lock_case_setup(self)

    def lock(self, recorder, user="Pau"):
        with patch.object(zoho_so_lock, "get_access_token", return_value="LOCK-TOKEN"), patch.object(zoho_so_lock.httpx, "request", recorder):
            return zoho_so_lock.lock_salesorder("4489499000262541103", user)

    def test_status_get_then_one_post_then_verify_get_exact_requests(self):
        rec = Recorder(response(body=so_payload(False)), response(body={"code": 0, "message": "success"}), response(body=so_payload(True)))
        result = self.lock(rec)
        self.assertTrue(result["locked"] and not result["already_locked"])
        self.assertEqual(rec.methods, ["GET", "POST", "GET"])
        get, post, verify = rec.calls
        self.assertEqual(get["url"], "https://www.zohoapis.com/inventory/v1/salesorders/4489499000262541103")
        self.assertEqual(get["params"], {"organization_id": "org-test"})
        self.assertEqual(post["url"], "https://www.zohoapis.com/inventory/v1/lock/4489499000111461258?entity=salesorder&entity_ids=4489499000262541103&organization_id=org-test")
        self.assertEqual(verify["url"], get["url"])
        self.assertTrue(all(c["headers"] == {"Authorization": "Zoho-oauthtoken LOCK-TOKEN"} for c in rec.calls))

    def test_body_is_form_encoded_with_one_jsonstring_field_not_json(self):
        rec = Recorder(response(body=so_payload(False)), response(body={"code": 0}), response(body=so_payload(True)))
        self.lock(rec)
        post = rec.calls[1]
        self.assertIsNone(post["json"])
        self.assertEqual(list(post["data"]), ["JSONString"])
        # What httpx would really put on the wire:
        wire = httpx.Request("POST", post["url"], data=post["data"])
        self.assertEqual(wire.headers["content-type"], "application/x-www-form-urlencoded")
        self.assertTrue(wire.content.decode().startswith("JSONString=%7B%22reason%22%3A%22Acknowledged+by+Supply+Chain+Department+via+IntelliFleet+-+Pau%22%7D"))
        self.assertNotIn(b'"JSONString"', wire.content)  # not a JSON document
        self.assertEqual(wire.content.decode().count("JSONString="), 1)

    def test_reason_is_the_base_text_plus_the_user_with_no_date(self):
        rec = Recorder(response(body=so_payload(False)), response(body={"code": 0}), response(body=so_payload(True)))
        self.lock(rec, user="Jed Gatmaitan")
        value = rec.calls[1]["data"]["JSONString"]
        self.assertEqual(json.loads(value), {"reason": "Acknowledged by Supply Chain Department via IntelliFleet - Jed Gatmaitan"})
        self.assertTrue(value.startswith('{"reason":"'))  # compact, exactly the Deluge shape

    def test_reason_base_is_56_characters_and_used_alone_without_a_user(self):
        self.assertEqual(len(zoho_so_lock.REASON_BASE), 56)
        for user in (None, "", "   "):
            self.assertEqual(zoho_so_lock.build_reason(user), zoho_so_lock.REASON_BASE)

    def test_reason_stays_within_90_characters_even_for_a_60_character_name(self):
        name = "Maria Cristina Dela Cruz-Santos de la Vega y Fernandez Reyes"[:60]
        self.assertEqual(len(name), 60)
        reason = zoho_so_lock.build_reason(name)
        self.assertLessEqual(len(reason), 90)
        self.assertTrue(reason.startswith(zoho_so_lock.REASON_BASE + " - Maria Cristina"))
        # measured AFTER json.dumps, as it travels in JSONString
        rec = Recorder(response(body=so_payload(False)), response(body={"code": 0}), response(body=so_payload(True)))
        self.lock(rec, user=name)
        sent = json.loads(rec.calls[1]["data"]["JSONString"])["reason"]
        self.assertLessEqual(len(json.dumps(sent, ensure_ascii=False)) - 2, 90)
        self.assertLessEqual(len(sent), 90)

    def test_reason_length_accounts_for_json_escaping(self):
        name = '"' * 60  # every quote becomes \" inside JSON
        reason = zoho_so_lock.build_reason(name)
        self.assertLessEqual(len(json.dumps(reason, ensure_ascii=False)) - 2, 90)

    def test_short_name_is_kept_whole_and_the_boundary_is_90(self):
        self.assertEqual(zoho_so_lock.build_reason("Pau"), "Acknowledged by Supply Chain Department via IntelliFleet - Pau")
        fits = "x" * (90 - 56 - 3)
        self.assertEqual(len(zoho_so_lock.build_reason(fits)), 90)
        self.assertEqual(len(zoho_so_lock.build_reason(fits + "y")), 90)  # one over: truncated back to 90

    def test_one_salesorder_per_call_and_malicious_ids_are_rejected(self):
        for bad in ("1,2", "1&entity_ids=2", "123/../x", "", "abc", "1 2"):
            rec = Recorder()
            with patch.object(zoho_so_lock, "get_access_token", return_value="T"), patch.object(zoho_so_lock.httpx, "request", rec):
                result = zoho_so_lock.lock_salesorder(bad, "Pau")
            self.assertFalse(result["locked"], bad)
            self.assertEqual(result["lock_error"], "invalid_salesorder_id")
            self.assertEqual(rec.calls, [], bad)  # not even a GET

    def test_lock_token_is_used_and_main_client_token_is_not(self):
        zoho_client._access_token = "MAIN-TOKEN"
        zoho_client._access_token_expires_at = 9e12
        tokens = []

        def fake_post(url, data=None, timeout=None):
            tokens.append(data["client_id"])
            return response(body={"access_token": "LOCK-ACCESS", "expires_in": 3600})

        rec = Recorder(response(body=so_payload(False)), response(body={"code": 0}), response(body=so_payload(True)))
        with patch.object(zoho_so_lock.httpx, "post", fake_post), patch.object(zoho_so_lock.httpx, "request", rec):
            zoho_so_lock.lock_salesorder("123", "Pau")
        self.assertEqual(tokens, ["lock-client"])  # one refresh, lock client only
        self.assertTrue(all(c["headers"]["Authorization"] == "Zoho-oauthtoken LOCK-ACCESS" for c in rec.calls))
        self.assertFalse(any("MAIN-TOKEN" in str(c["headers"]) for c in rec.calls))

    def test_already_locked_by_get_makes_no_post(self):
        rec = Recorder(response(body=so_payload(True)))
        result = self.lock(rec)
        self.assertEqual((result["locked"], result["already_locked"]), (True, True))
        self.assertEqual(rec.methods, ["GET"])

    def test_zoho_already_locked_error_on_post_counts_as_success(self):
        rec = Recorder(response(body=so_payload(False)), response(status=400, body={"code": 9999, "message": "The sales order is already locked."}), response(body=so_payload(True)))
        result = self.lock(rec)
        self.assertTrue(result["locked"] and result["already_locked"])

    def test_post_failures_return_locked_false_with_zoho_message_and_skip_verify(self):
        for post in (response(status=500, body={"message": "Server error"}), response(status=200, body={"code": 1234, "message": "Invalid value"})):
            rec = Recorder(response(body=so_payload(False)), post)
            result = self.lock(rec)
            self.assertFalse(result["locked"])
            self.assertIn(result["lock_error"], {"Server error", "Invalid value"})
            self.assertEqual(rec.methods, ["GET", "POST"])  # no verify after a failed POST

    def test_verify_with_unlocked_or_wrong_config_is_not_locked(self):
        for verified in (so_payload(False), so_payload(True, config_id="999")):
            rec = Recorder(response(body=so_payload(False)), response(body={"code": 0}), response(body=verified))
            result = self.lock(rec)
            self.assertFalse(result["locked"])
            self.assertEqual(result["lock_error"], "lock_verify_failed")
            self.assertEqual(rec.methods, ["GET", "POST", "GET"])

    def test_scope_errors_map_to_lock_credential_lacks_scope(self):
        cases = [
            ("GET 401", [response(status=401, body={"code": 57, "message": "You are not authorized to perform this operation"})]),
            ("POST 403", [response(body=so_payload(False)), response(status=403, body={"message": "forbidden"})]),
            ("POST code 57", [response(body=so_payload(False)), response(status=200, body={"code": 57, "message": "x"})]),
            ("POST scope text", [response(body=so_payload(False)), response(status=400, body={"message": "Invalid OAuth scope"})]),
        ]
        for name, responses in cases:
            result = self.lock(Recorder(*responses))
            self.assertFalse(result["locked"], name)
            self.assertEqual(result["lock_error"], "lock_credential_lacks_scope", name)

    def test_lock_never_raises_on_connection_or_token_problems(self):
        rec = Recorder(httpx.ConnectError("down"))
        result = self.lock(rec)
        self.assertEqual((result["locked"], result["lock_error"]), (False, "lock_connection_error"))
        rec = Recorder(response(body=so_payload(False)), httpx.ReadTimeout("slow"))
        self.assertEqual(self.lock(rec)["lock_error"], "lock_connection_error")
        # refresh token rejected by Zoho accounts
        zoho_so_lock.reset_token_cache()
        with patch.object(zoho_so_lock.httpx, "post", lambda *a, **k: response(status=400, body={"error": "invalid_code"})), patch.object(zoho_so_lock.httpx, "request", Recorder()):
            result = zoho_so_lock.lock_salesorder("123", "Pau")
        self.assertEqual((result["locked"], result["lock_error"]), (False, "lock_credential_needs_reauthentication"))

    def test_requests_go_through_the_shared_zoho_throttle(self):
        admitted = []
        limiter = zoho_rate_limiter.limiter_for("org-test")
        real_admit = limiter.admit

        def counting_admit():
            admitted.append(1)
            return real_admit()

        rec = Recorder(response(body=so_payload(False)), response(body={"code": 0}), response(body=so_payload(True)))
        with patch.object(limiter, "admit", counting_admit):
            self.lock(rec)
        self.assertEqual(len(admitted), 3)  # GET, POST, verify GET

    def test_health_and_configuration_flags(self):
        self.assertEqual(zoho_so_lock.health_status(), "ok")
        for name in LOCK_ENV:
            with patch.dict(os.environ, {name: ""}, clear=False):
                self.assertEqual(zoho_so_lock.health_status(), "not_configured", name)
                self.assertFalse(zoho_so_lock.configured())

    def test_not_configured_logs_a_warning_at_startup(self):
        with patch.dict(os.environ, {"ZOHO_LOCK_REFRESH_TOKEN": ""}, clear=False):
            with self.assertLogs("zoho", level="WARNING") as logs:
                zoho_so_lock.log_configuration_warning()
        self.assertIn("so_lock: not_configured", "\n".join(logs.output))

    def test_secrets_and_tokens_never_appear_in_logs(self):
        zoho_so_lock.reset_token_cache()
        secrets = ["lock-secret-VALUE", "lock-refresh-VALUE", "LOCK-ACCESS-SECRET", "Zoho-oauthtoken"]
        calls = [response(body=so_payload(False)), response(status=403, body={"message": "not authorized"})]
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("zoho").info("start")  # assertLogs needs at least one record
            with patch.object(zoho_so_lock.httpx, "post", lambda *a, **k: response(body={"access_token": "LOCK-ACCESS-SECRET", "expires_in": 3600})), patch.object(zoho_so_lock.httpx, "request", Recorder(*calls)):
                result = zoho_so_lock.lock_salesorder("123", "Pau")
                load_planning._lock_after_acknowledge("123", "SO-1", SimpleNamespace(full_name="Pau"))  # router-side ERROR log path
        self.assertFalse(result["locked"])
        text = "\n".join(logs.output)
        for secret in secrets:
            self.assertNotIn(secret, text)


class LockErrorDetailTests(unittest.TestCase):
    def setUp(self):
        lock_case_setup(self)

    def lock(self, recorder):
        with patch.object(zoho_so_lock, "get_access_token", return_value="LOCK-TOKEN"), patch.object(zoho_so_lock.httpx, "request", recorder):
            return zoho_so_lock.lock_salesorder("123", "Pau")

    def test_post_failure_reports_the_raw_http_status_and_zoho_code(self):
        result = self.lock(Recorder(response(body=so_payload(False)), response(status=400, body={"code": 4, "message": "reason has less than 100 characters"})))
        self.assertEqual((result["lock_http_status"], result["lock_zoho_code"], result["lock_error"]), (400, 4, "reason has less than 100 characters"))

    def test_scope_error_and_verify_failure_also_carry_them(self):
        scope = self.lock(Recorder(response(body=so_payload(False)), response(status=403, body={"code": 57, "message": "not authorized"})))
        self.assertEqual((scope["lock_http_status"], scope["lock_zoho_code"]), (403, 57))
        verify = self.lock(Recorder(response(body=so_payload(False)), response(body={"code": 0}), response(body=so_payload(False))))
        self.assertEqual((verify["lock_error"], verify["lock_http_status"], verify["lock_zoho_code"]), ("lock_verify_failed", 200, 0))

    def test_success_carries_them_and_router_error_log_includes_them(self):
        ok = self.lock(Recorder(response(body=so_payload(False)), response(body={"code": 0}), response(body=so_payload(True))))
        self.assertEqual((ok["lock_http_status"], ok["lock_zoho_code"]), (200, 0))
        failed = {"locked": False, "lock_error": "boom", "lock_http_status": 400, "lock_zoho_code": 4}
        with patch.object(load_planning, "lock_salesorder", return_value=failed), self.assertLogs("load_planning", level="ERROR") as logs:
            payload = load_planning._lock_after_acknowledge("123", "SO-1", SimpleNamespace(full_name="Pau"))
        self.assertEqual((payload["lock_http_status"], payload["lock_zoho_code"]), (400, 4))
        self.assertIn("lock_http_status=400", logs.output[0])
        self.assertIn("lock_zoho_code=4", logs.output[0])


class LockRouterBehaviourTests(unittest.TestCase):
    def setUp(self):
        lock_case_setup(self)
        self.user = CurrentUser(id=1, email="pau@example.com", role="dispatcher", full_name="Pau", status="active")
        self.cached = SimpleNamespace(order_status="confirmed", salesorder_number="SO-1", raw_json={})

    def ack(self, **patches):
        stack = [
            patch.object(load_planning.live_sales_order_cache, "find_cached", return_value=self.cached),
            patch.object(load_planning.live_sales_order_cache, "mark_acknowledged"),
            patch.object(load_planning, "_set_acknowledged"),
            patch.object(load_planning, "_is_acknowledged", return_value=False),
        ]
        for name, value in patches.items():
            stack.append(patch.object(load_planning, name, **value))
        mocks = {}
        for p in stack:
            mocks[p.attribute] = p.start()
            self.addCleanup(p.stop)
        return mocks

    def test_acknowledge_failure_returns_502_and_never_calls_lock(self):
        mocks = self.ack(acknowledge_sales_order={"side_effect": zoho_client.ZohoError("ack failed")}, lock_salesorder={})
        with self.assertRaises(HTTPException) as caught:
            load_planning.acknowledge_sales_order_route("1", self.user)
        self.assertEqual(caught.exception.status_code, 502)
        mocks["lock_salesorder"].assert_not_called()

    def test_lock_post_failure_keeps_the_acknowledge_and_logs_error(self):
        self.ack(acknowledge_sales_order={"return_value": {"code": 0}}, lock_salesorder={"return_value": {"locked": False, "lock_error": "Invalid value"}})
        with self.assertLogs("load_planning", level="ERROR") as logs:
            body = load_planning.acknowledge_sales_order_route("1", self.user)
        self.assertEqual((body["acknowledged"], body["locked"], body["lock_error"], body["so_number"], body["so_id"]), (True, False, "Invalid value", "SO-1", "1"))
        self.assertIn("lock failed", logs.output[0])

    def test_an_unexpected_lock_exception_never_turns_a_good_acknowledge_into_502(self):
        self.ack(acknowledge_sales_order={"return_value": {"code": 0}}, lock_salesorder={"side_effect": RuntimeError("boom")})
        with self.assertLogs("load_planning", level="ERROR"):
            body = load_planning.acknowledge_sales_order_route("1", self.user)
        self.assertTrue(body["acknowledged"])
        self.assertEqual((body["locked"], body["lock_error"]), (False, "lock_unexpected_error"))

    def test_missing_lock_env_still_acknowledges_with_not_configured(self):
        self.ack(acknowledge_sales_order={"return_value": {"code": 0}})  # real lock_salesorder, blank env
        with patch.dict(os.environ, {"ZOHO_LOCK_CLIENT_ID": ""}, clear=False):
            with patch.object(zoho_so_lock.httpx, "request", Recorder()) as http, self.assertLogs("load_planning", level="ERROR"):
                body = load_planning.acknowledge_sales_order_route("1", self.user)
        self.assertEqual((body["acknowledged"], body["locked"], body["lock_error"]), (True, False, "not_configured"))
        self.assertEqual(http.calls, [])

    def test_acknowledge_then_lock_end_to_end_uses_the_logged_in_user_in_the_reason(self):
        self.ack(acknowledge_sales_order={"return_value": {"code": 0}})
        rec = Recorder(response(body=so_payload(False)), response(body={"code": 0}), response(body=so_payload(True)))
        with patch.object(zoho_so_lock, "get_access_token", return_value="LOCK-TOKEN"), patch.object(zoho_so_lock.httpx, "request", rec):
            body = load_planning.acknowledge_sales_order_route("123", self.user)
        self.assertEqual((body["acknowledged"], body["locked"]), (True, True))
        self.assertIn("Pau", json.loads(rec.calls[1]["data"]["JSONString"])["reason"])
        self.assertEqual(body["lock_status"]["config_name"], "For Fulfillment")

    def test_bulk_one_failure_never_stops_the_others(self):
        rows = [SimpleNamespace(id=str(n), order_status="confirmed", salesorder_number=f"SO-{n}", raw_json={}) for n in (1, 2, 3)]
        acks = [{"code": 0}, zoho_client.ZohoError("ack failed"), {"code": 0}]
        locks = [{"locked": True, "lock_error": None}, {"locked": False, "lock_error": "Invalid value"}]
        with patch.object(load_planning, "_filtered_rows", return_value=rows), \
                patch.object(load_planning, "acknowledge_sales_order", side_effect=acks), \
                patch.object(load_planning, "lock_salesorder", side_effect=locks) as lock, \
                patch.object(load_planning.live_sales_order_cache, "mark_acknowledged"), \
                patch.object(load_planning, "_set_acknowledged"), patch.object(load_planning, "_is_acknowledged", return_value=False), self.assertLogs("load_planning", level="ERROR"):
            body = load_planning.acknowledge_filtered_sales_orders(db=SimpleNamespace(), current_user=self.user)
        self.assertEqual(lock.call_count, 2)  # SO-2's acknowledge failed: never locked
        self.assertEqual((body["acknowledged_count"], body["failed_count"]), (2, 1))
        by_number = {r["so_number"]: r for r in body["results"]}
        self.assertEqual((by_number["SO-1"]["acknowledged"], by_number["SO-1"]["locked"]), (True, True))
        self.assertEqual((by_number["SO-2"]["acknowledged"], by_number["SO-2"]["locked"]), (False, False))
        self.assertEqual((by_number["SO-3"]["acknowledged"], by_number["SO-3"]["locked"], by_number["SO-3"]["lock_error"]), (True, False, "Invalid value"))

    def test_retry_route_locks_one_so_for_the_user_without_acknowledging(self):
        with patch.object(load_planning.live_sales_order_cache, "find_cached", return_value=self.cached), \
                patch.object(load_planning, "acknowledge_sales_order") as ack, \
                patch.object(load_planning, "lock_salesorder", return_value={"locked": True, "already_locked": False, "lock_status": {"is_locked": True}, "lock_error": None}) as lock:
            body = load_planning.lock_sales_order_route("123", self.user)
        lock.assert_called_once_with("123", user="Pau")
        ack.assert_not_called()
        self.assertEqual((body["so_id"], body["so_number"], body["locked"]), ("123", "SO-1", True))

    def test_retry_route_has_the_same_roles_as_acknowledge(self):
        import inspect

        def roles(fn):
            default = inspect.signature(fn).parameters["current_user"].default
            return default.dependency.__closure__ and [c.cell_contents for c in default.dependency.__closure__]

        self.assertEqual(roles(load_planning.lock_sales_order_route), roles(load_planning.acknowledge_sales_order_route))
        paths = {route.path: route for route in load_planning.router.routes}
        self.assertIn("/api/load-planning/salesorders/{salesorder_id}/lock", paths)
        self.assertEqual(paths["/api/load-planning/salesorders/{salesorder_id}/lock"].methods, {"POST"})


class ListAndDetailDoNotCallZohoLockTests(unittest.TestCase):
    def setUp(self):
        lock_case_setup(self)

    def test_list_summaries_make_no_lock_calls_and_read_lock_state_from_the_row(self):
        from services import live_sales_order_cache

        records = [
            {"salesorder_id": str(100 + n), "salesorder_number": f"SO-{n}", "customer_name": "C", "status": "confirmed", "date": "2026-10-05",
             **({"lock_details": {"is_locked": True, "locking_config_id": CONFIG_ID, "locking_config_name": "For Fulfillment", "locked_by": "Pau", "lock_time": "t"}} if n == 0 else {})}
            for n in range(5)
        ]
        rows = [live_sales_order_cache._build_transient(record) for record in records]

        def forbidden(*args, **kwargs):
            raise AssertionError("the list must not call Zoho for locks")

        with patch.object(zoho_so_lock.httpx, "request", forbidden), patch.object(zoho_so_lock.httpx, "post", forbidden), \
                patch.object(zoho_so_lock, "get_lock_status", forbidden):
            summaries = [load_planning._summary(row, db=None, allow_fetch=False) for row in rows]
        self.assertEqual([s["zoho_lock"]["is_locked"] for s in summaries], [True, False, False, False, False])
        self.assertEqual(summaries[0]["zoho_lock"]["config_name"], "For Fulfillment")

    def test_drawer_reads_lock_state_from_the_detail_record_with_no_extra_zoho_call(self):
        record = {"salesorder_id": "123", "salesorder_number": "SO-1", "status": "confirmed",
                  "lock_details": {"is_locked": True, "locking_config_id": CONFIG_ID, "locking_config_name": "For Fulfillment", "locked_by": "Pau", "lock_time": "t", "lock_reason": "Acknowledged by Supply Chain Department via IntelliFleet (Pau, 2026-10-05 09:22 Asia/Manila)"}}

        def forbidden(*args, **kwargs):
            raise AssertionError("opening the drawer must not call Zoho for the lock")

        with patch.object(load_planning, "fetch_sales_order_detail", return_value={"salesorder": record}) as fetch,                 patch.object(load_planning.live_sales_order_cache, "publish_zoho_data"),                 patch.object(load_planning.live_sales_order_cache, "find_cached", return_value=None),                 patch.object(load_planning, "sales_order_delivery_status", return_value={}),                 patch.object(zoho_so_lock.httpx, "request", forbidden), patch.object(zoho_so_lock.httpx, "post", forbidden):
            body = load_planning.get_sales_order("123")
        fetch.assert_called_once()  # still exactly one detail GET
        self.assertEqual((body["zoho_lock"]["is_locked"], body["zoho_lock"]["config_name"], body["zoho_lock"]["locked_by"]), (True, "For Fulfillment", "Pau"))
        self.assertIn("Acknowledged by Supply Chain Department", body["zoho_lock"]["reason"])
        self.assertEqual(body["salesorder"]["salesorder_number"], "SO-1")

    def test_drawer_shows_unlocked_when_the_record_has_no_lock_details(self):
        with patch.object(load_planning, "fetch_sales_order_detail", return_value={"salesorder": {"salesorder_id": "1", "status": "confirmed"}}),                 patch.object(load_planning.live_sales_order_cache, "publish_zoho_data"),                 patch.object(load_planning.live_sales_order_cache, "find_cached", return_value=None),                 patch.object(load_planning, "sales_order_delivery_status", return_value={}):
            body = load_planning.get_sales_order("1")
        self.assertFalse(body["zoho_lock"]["is_locked"])

if __name__ == "__main__":
    unittest.main()
