from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jose import jwt

import main
from auth import security
from auth.dependencies import CurrentUser
from routers import orders
from services import memory_tables


def _token() -> str:
    return jwt.encode(
        {"sub": "1", "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        security.JWT_SECRET,
        algorithm=security.JWT_ALGORITHM,
    )


@pytest.fixture
def proof_env(tmp_path, monkeypatch):
    user = CurrentUser(1, "admin@example.test", "admin", "Admin", "active")
    monkeypatch.setattr("auth.dependencies._load_user", lambda _user_id: user)
    monkeypatch.setattr(main, "_load_user", lambda _user_id: user)
    monkeypatch.setattr(orders, "PRIVATE_MEDIA_ROOT", str(tmp_path / "private" / "delivery_proofs"))
    memory_tables.orders._rows.clear()
    memory_tables.delivery_proofs._rows.clear()
    memory_tables.orders._next_id = 1
    memory_tables.delivery_proofs._next_id = 1
    yield user
    memory_tables.orders._rows.clear()
    memory_tables.delivery_proofs._rows.clear()


def test_delivery_proof_requires_auth_and_authorizes_order(proof_env):
    order = memory_tables.orders.create(customer_id="customer-a")
    client = TestClient(main.app)
    token = _token()

    created = client.post(
        f"/orders/{order['id']}/proof",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("receipt.txt", b"synthetic-proof", "text/plain")},
    )
    assert created.status_code == 201
    proof = created.json()
    assert proof["photo_url"] == f"/orders/{order['id']}/proof/{proof['id']}"

    assert client.get(proof["photo_url"]).status_code == 401
    fetched = client.get(proof["photo_url"], headers={"Authorization": f"Bearer {token}"})
    assert fetched.status_code == 200
    assert fetched.content == b"synthetic-proof"

    other_order = memory_tables.orders.create(customer_id="customer-b")
    cross_order = client.get(
        f"/orders/{other_order['id']}/proof/{proof['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert cross_order.status_code == 404


def test_missing_proof_file_and_traversal_filename_are_safe(proof_env, tmp_path):
    order = memory_tables.orders.create(customer_id="customer-a")
    client = TestClient(main.app)
    token = _token()
    created = client.post(
        f"/orders/{order['id']}/proof",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("../../escape.txt", b"synthetic-proof", "text/plain")},
    )
    assert created.status_code == 201
    proof = created.json()
    stored = Path(memory_tables.delivery_proofs.get(proof["id"])["storage_path"]).resolve()
    root = (tmp_path / "private" / "delivery_proofs").resolve()
    assert root in stored.parents
    assert not (tmp_path / "escape.txt").exists()

    stored.unlink()
    missing = client.get(proof["photo_url"], headers={"Authorization": f"Bearer {token}"})
    assert missing.status_code == 404


def test_public_static_mount_rejects_delivery_proof_paths(proof_env):
    public_root = Path(main.media_root) / "delivery_proofs"
    public_root.mkdir(parents=True, exist_ok=True)
    synthetic = public_root / "_security_test_synthetic.txt"
    synthetic.write_bytes(b"synthetic-only")
    try:
        response = TestClient(main.app).get("/media/delivery_proofs/_security_test_synthetic.txt")
        assert response.status_code == 404
    finally:
        synthetic.unlink(missing_ok=True)

