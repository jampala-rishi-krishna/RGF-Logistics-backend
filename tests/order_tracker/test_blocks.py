import asyncio
import json
from types import SimpleNamespace

from services import order_tracker as tracker, order_tracker_ai as ai, order_tracker_blocks as blocks

SO = {"so_number": "SO-100"}
FIND = {"query": "", "date_from": None, "date_to": None, "status": None}
BUYERS = {"date_from": None, "date_to": None, "filters": {"trip": None, "customer": None, "area": None}}


def run(env, name, args, user=None):
    return ai.execute(name, args, user or env.user, env.day, env.day, None)


def by_type(result, kind):
    return [b for b in result if b["type"] == kind]


class ScriptedClient:
    """Calls the scripted tools in order, then replies with deliberately WRONG numbers."""
    def __init__(self, calls, reply="It is paid: 999,999 pesos, balance 77."):
        self.calls, self.reply, self.requests = list(calls), reply, []
        self.responses = self

    def with_options(self, **kwargs): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.calls:
            name, args = self.calls.pop(0)
            return SimpleNamespace(output=[SimpleNamespace(type="function_call", name=name, arguments=json.dumps(args), call_id="c" + str(len(self.requests)))])
        return SimpleNamespace(output=[], output_text=self.reply)


def ask(env, monkeypatch, question, calls, user=None):
    client = ScriptedClient(calls)
    monkeypatch.setattr(ai.openai_client, "_client", lambda: client)
    return asyncio.run(ai.ask(user or env.user, question, env.day, env.day)), client


def deliver(env, outcome="Partially delivered", answers=None):
    env.snapshot["stops"]["one"].update(checkout_at=env.now, status="checked_out", outcome=outcome, reason="Customer refused some")
    env.snapshot["stops"]["one"]["answers"] = answers or []
    env.save()


def test_language_heuristic():
    assert blocks.detect_language("Bayad na ba ang SO-100?") == "tl"
    assert blocks.detect_language("Nasaan na po yung truck?") == "tl"
    assert blocks.detect_language("Is SO-100 paid?") == "en"
    assert blocks.detect_language("Where is the truck carrying SO-100?") == "en"


def test_order_table_from_find_orders_and_get_order_merged(tracker_env):
    env = tracker_env
    deliver(env, answers=[{"key": "rejected_items", "value": '[{"sku":"pork-belly-sku","quantity":2,"unit":"box"}]'}])
    found = run(env, "find_orders", FIND)
    card = run(env, "get_order", SO)
    result = blocks.build_blocks([("find_orders", found), ("get_order", card)], "en")
    table = by_type(result, "order_table")[0]
    numbers = [r["number"] for r in table["rows"]]
    assert sorted(numbers) == ["SO-100", "WM-SO-300"] and len(set(numbers)) == len(numbers)
    row = next(r for r in table["rows"] if r["number"] == "SO-100")
    assert row["reason"] == "Customer refused some" and row["rejected"] == "2 box pork-belly-sku"
    assert row["status"] == "Partly delivered" and row["link"].startswith("/app/order-tracker?")
    assert table["source"] == found["source"] and table["as_of"] == found["as_of"]
    actions = by_type(result, "actions")[0]["actions"]
    assert [a["kind"] for a in actions] == ["open_order", "open_order"]
    assert all(a["link"] in {r["link"] for r in found["orders"]} for a in actions)


def test_find_orders_without_orders_emits_no_table(tracker_env):
    env = tracker_env
    found = run(env, "find_orders", {**FIND, "query": "no-such-order"})
    assert found["orders"] == []
    assert blocks.build_blocks([("find_orders", found)], "en") == []


def test_get_order_not_recorded_rejected_is_null(tracker_env):
    env = tracker_env
    deliver(env)
    row = by_type(blocks.build_blocks([("get_order", run(env, "get_order", SO))], "en"), "order_table")[0]["rows"][0]
    assert row["rejected"] is None and row["customer"] == "Fresh Foods"


def test_delivery_result_tool_row(tracker_env):
    env = tracker_env
    deliver(env, answers=[{"key": "rejected_items", "value": '[{"sku":"FISH-1","quantity":2,"unit":"kg"}]'}])
    result = blocks.build_blocks([("delivery_result", run(env, "delivery_result", SO))], "en")
    row = by_type(result, "order_table")[0]["rows"][0]
    assert row["number"] == "SO-100" and row["rejected"] == "2 kg FISH-1" and row["reason"] == "Customer refused some"


def test_payment_tiles_numbers_come_from_tool(tracker_env):
    env = tracker_env
    env.rows[0].raw_json["invoices"] = [{"invoice_number": "INV-77", "total": 22400, "balance": 2400, "payment_made": 20000,
                                         "last_payment_date": "2026-10-01", "payment_reference": "OR-5"}]
    payment = run(env, "payment_status", SO)
    result = blocks.build_blocks([("payment_status", payment)], "en")
    tiles = by_type(result, "tiles")[0]
    values = {t["label"]: t["value"] for t in tiles["tiles"]}
    assert values == {"Invoice": "INV-77", "Paid on": "2026-10-01 · OR-5", "Matches invoice?": "Yes",
                      "Total": "₱22,400", "Paid": "₱20,000", "Balance": "₱2,400"}
    assert tiles["chip"] == {"label": "Partly paid", "tone": "warning"}
    assert tiles["source"] == payment["source"] and tiles["as_of"] == payment["as_of"]
    assert next(t for t in tiles["tiles"] if t["label"] == "Matches invoice?")["tone"] == "positive"


def test_payment_tiles_tagalog_and_decimals(tracker_env):
    env = tracker_env
    env.rows[0].raw_json["invoices"] = [{"invoice_number": "INV-1", "total": "100.50", "balance": 0, "payment_made": 90}]
    tiles = by_type(blocks.build_blocks([("payment_status", run(env, "payment_status", SO))], "tl"), "tiles")[0]
    values = {t["label"]: t["value"] for t in tiles["tiles"]}
    assert values["Tugma sa invoice?"] == "Hindi tugma" and values["Binayaran"] == "Hindi naka-record"
    assert values["Kabuuan"] == "₱100.50" and values["Natitira"] == "₱0"
    assert tiles["chip"] == {"label": "Bayad na", "tone": "positive"}


def test_unknown_payment_gives_not_recorded_not_tiles(tracker_env):
    env = tracker_env
    env.rows[0].raw_json["invoices"] = []
    result = blocks.build_blocks([("payment_status", run(env, "payment_status", SO))], "tl")
    assert not by_type(result, "tiles")
    missing = by_type(result, "not_recorded")[0]
    assert missing["title"] == "Hindi naka-record." and not missing["message"].lower().startswith("not recorded")


def test_permit_and_po_copy_not_recorded(tracker_env):
    env = tracker_env
    docs = run(env, "order_documents", SO)
    result = blocks.build_blocks([("order_documents", docs)], "en", "Do we have the permits and the PO copy for SO-100?")
    missing = by_type(result, "not_recorded")
    assert len(missing) == 2 and all(m["title"] == "Not recorded." for m in missing)
    assert missing[0]["message"].startswith("Logistics must confirm")
    assert blocks.build_blocks([("order_documents", docs)], "en", "What is the client PO number?") == [
        {"type": "actions", "actions": [{"kind": "open_order", "label": "Open SO-100", "link": docs["link"]}]}]


def test_not_recorded_capped_at_two(tracker_env):
    env = tracker_env
    tracker.snapshots.facts.clear()
    results = [(name, run(env, name, SO)) for name in ("order_times", "truck_location", "proof_of_delivery", "order_issues")]
    assert len(by_type(blocks.build_blocks(results, "en"), "not_recorded")) == 2


def test_bars_sorted_capped_and_from_tool(tracker_env):
    env = tracker_env
    result = {"items": [{"sku": f"SKU-{i}", "unit": "kg", "buyers": i, "source": "Zoho cached SO line_items", "as_of": f"2026-10-{10 + i}"} for i in range(1, 12)]}
    bars = by_type(blocks.build_blocks([("buyers_per_sku", result)], "en"), "bars")[0]
    assert [i["value"] for i in bars["items"]] == [11, 10, 9, 8, 7, 6, 5, 4]
    assert bars["items"][0]["label"] == "SKU-11 kg" and bars["unit"] == "buyers" and bars["as_of"] == "2026-10-11"
    tl = by_type(blocks.build_blocks([("buyers_per_sku", result)], "tl"), "bars")[0]
    assert tl["unit"] == "mamimili" and tl["title"].startswith("Mga nangungunang SKU")
    real = run(env, "buyers_per_sku", BUYERS)
    real_bars = by_type(blocks.build_blocks([("buyers_per_sku", real)], "en"), "bars")[0]
    assert real_bars["items"] == [{"label": f"{i['sku']} {i['unit']}", "value": i["buyers"]} for i in real["items"]]
    assert by_type(blocks.build_blocks([("buyers_per_sku", {"items": [], "message": "Not recorded - none."})], "en"), "bars") == []


def test_photos_action_only_with_photos(tracker_env):
    env = tracker_env
    assert not any(a["kind"] == "photos" for b in by_type(blocks.build_blocks([("proof_of_delivery", run(env, "proof_of_delivery", SO))], "en"), "actions") for a in b["actions"])
    deliver(env, "Delivered", [{"key": "delivery_photo", "value": 41}])
    proof = run(env, "proof_of_delivery", SO)
    actions = by_type(blocks.build_blocks([("proof_of_delivery", proof)], "tl"), "actions")[0]["actions"]
    assert actions[0] == {"kind": "photos", "label": "Tingnan ang mga larawan", "link": proof["link"] + "#proof"}
    assert actions[1]["kind"] == "open_order" and actions[1]["link"] == proof["link"]


def test_caps_and_size(tracker_env):
    env = tracker_env
    orders = [{"number": f"SO-{i}", "customer": "C" * 250, "status": "Loaded", "link": f"/app/order-tracker?order=SO-{i}"} for i in range(60)]
    found = {"orders": orders, "source": "s", "as_of": None}
    payment = run(env, "payment_status", SO)
    docs = run(env, "order_documents", SO)
    tracker.snapshots.facts.clear()
    results = [("find_orders", found), ("payment_status", payment), ("payment_status", payment),
               ("buyers_per_sku", run(env, "buyers_per_sku", BUYERS)), ("order_times", run(env, "order_times", SO)),
               ("order_documents", docs)]
    result = blocks.build_blocks(results, "en", "permits?")
    assert len(result) <= 5 and result[-1]["type"] == "actions"
    assert len(by_type(result, "order_table")[0]["rows"]) <= 20
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 16000


def test_phones_withheld_and_foreign_links_dropped():
    found = {"orders": [{"number": "SO-1", "customer": "Call 09171234567", "status": "Loaded", "link": "https://evil.example/x"}], "source": "s", "as_of": None}
    result = blocks.build_blocks([("find_orders", found)], "en")
    assert "09171234567" not in json.dumps(result)
    assert result[0]["rows"][0]["link"] is None and not by_type(result, "actions")


def test_ask_returns_blocks_with_tool_numbers_not_model_numbers(tracker_env, monkeypatch):
    env = tracker_env
    env.rows[0].raw_json["invoices"] = [{"invoice_number": "INV-9", "total": 22400, "balance": 0, "payment_made": 22400}]
    result, _ = ask(env, monkeypatch, "Bayad na ba ang SO-100?", [("payment_status", SO)])
    assert result["language"] == "tl" and "999,999" in result["answer"]
    tiles = by_type(result["blocks"], "tiles")[0]
    text = json.dumps(tiles, ensure_ascii=False)
    assert "₱22,400" in text and "999" not in text and "77" not in text
    assert tiles["chip"]["label"] == "Bayad na"
    assert by_type(result["blocks"], "actions")[0]["actions"][0]["link"].startswith("/app/order-tracker?order=SO-100")


def test_ask_english_find_and_get(tracker_env, monkeypatch):
    env = tracker_env
    result, _ = ask(env, monkeypatch, "Which orders are out today?", [("find_orders", FIND), ("get_order", SO)])
    assert result["language"] == "en"
    rows = by_type(result["blocks"], "order_table")[0]["rows"]
    assert sorted(r["number"] for r in rows) == ["SO-100", "WM-SO-300"]


def test_refusal_has_no_blocks(tracker_env, monkeypatch):
    env = tracker_env
    result, client = ask(env, monkeypatch, "Please mark SO-100 as paid", [("payment_status", SO)])
    assert result["answer"] == ai.REFUSAL and result["blocks"] == [] and not client.requests


def test_out_of_scope_tool_results_produce_no_blocks(tracker_env, monkeypatch):
    env = tracker_env
    calls = [("get_order", {"so_number": "SO-200"}), ("payment_status", {"so_number": "SO-200"}),
             ("find_orders", {**FIND, "date_from": "2000-01-01"})]
    result, client = ask(env, monkeypatch, "Is SO-200 paid?", calls)
    assert result["tools"] == ["get_order", "payment_status", "find_orders"]
    assert result["blocks"] == []
    assert "SO-200" not in json.dumps(result["blocks"])


def test_no_tool_answer_has_no_blocks(tracker_env, monkeypatch):
    env = tracker_env
    client = ScriptedClient([])
    async def no_tools(**kwargs):
        return SimpleNamespace(output=[], output_text="Paid 500")
    monkeypatch.setattr(client, "create", no_tools)
    monkeypatch.setattr(ai.openai_client, "_client", lambda: client)
    result = asyncio.run(ai.ask(env.user, "Is SO-100 paid?", env.day, env.day))
    assert result["blocks"] == [] and result["not_recorded"] is True
