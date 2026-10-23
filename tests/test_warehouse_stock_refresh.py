from services import item_detail_cache as items
from services import warehouse_stock
from services import zoho_client


class InlinePool:
    def submit(self, fn, *args):
        fn(*args)


def test_stock_refresh_falls_back_to_item_detail_when_batch_omits_warehouses(monkeypatch):
    calls = []

    def request(method, path, params, feature):
        calls.append(path)
        if path == "items":
            return {"items": [{"item_id": "a", "package_details": {"weight": 1}}]}
        if path == "items/a":
            return {"item": {"item_id": "a", "warehouses": [
                {"warehouse_id": "4489499000064563124", "warehouse_actual_available_for_sale_stock": 177},
                {"warehouse_id": "4489499000064711027", "warehouse_actual_available_for_sale_stock": 226},
            ]}}
        return {"item": {"item_id": path.rsplit("/", 1)[-1], "warehouses": []}}

    monkeypatch.setattr(zoho_client, "_request_with_feature", request)
    monkeypatch.setattr(items, "_pool", InlinePool())
    monkeypatch.setattr(items, "_items", {})
    monkeypatch.setattr(items, "_failed", {})
    monkeypatch.setattr(items, "_inflight", set())

    items.request_refresh(["a"], require_stock=True)

    assert calls == ["items", "items/a"]
    assert warehouse_stock.cached_item_stock("a", "4489499000001322444") == {"mets": 177, "glacier": 226}
