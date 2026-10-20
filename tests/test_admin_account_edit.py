import pytest
from sqlalchemy import select

from auth.security import hash_password, verify_password
from models.user import User, UserInvite
from routers import admin, auth
from services import user_invites
from tests import trip_test_support as t


@pytest.fixture
def env(monkeypatch):
    env = t.build_env(monkeypatch, admin.usage_router, auth.router)
    env.admin = t.make_user(env, name="Admin", email="admin@example.test", role="admin")
    env.helper = t.make_user(env, name="Test Helper", email="fake@example.test", role="helper", staff_id=17, status="disabled")
    env.headers = t.bearer(env.admin, "admin")
    yield env
    assert not env.zoho_calls
    env.engine.dispose()


def test_correct_disabled_test_account_preserves_identity_and_allows_new_password(env):
    with env.Session() as db:
        user = db.get(User, env.helper)
        user.password_hash = hash_password("old-test-password")
        old_token, _ = user_invites.create_invite(db, user, "invite")
        db.commit()
    url = f"/api/admin/users/{env.helper}"
    body = {"full_name": "Real Helper", "email": " REAL@example.test ", "phone": None, "status": "active", "staff_id": 99, "role": "admin"}
    response = env.client.patch(url, headers=env.headers, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["email"] == "real@example.test" and result["password_reset_required"]
    assert result["id"] == env.helper and result["staff_id"] == 17 and result["role"] == "helper"
    with env.Session() as db:
        user = db.get(User, env.helper)
        assert user.status == "active" and not verify_password("old-test-password", user.password_hash)
        assert user_invites.redeem(db, old_token) is None
        token, _ = user_invites.create_invite(db, user, "reset")
        db.commit()
    assert env.client.post("/auth/set-password", json={"token": token, "password": "new-real-password"}).status_code == 200
    login = env.client.post("/auth/login", json={"identifier": "real@example.test", "password": "new-real-password"})
    assert login.status_code == 200, login.text
    assert login.json()["user"]["id"] == env.helper
    # Saving contact details with the same email must not reset the real password.
    body["email"] = "real@example.test"
    assert not env.client.patch(url, headers=env.headers, json=body).json()["password_reset_required"]
    assert env.client.post("/auth/login", json={"identifier": "real@example.test", "password": "new-real-password"}).status_code == 200


def test_edit_is_admin_only_and_rejects_duplicates_and_self_disable(env):
    url = f"/api/admin/users/{env.helper}"
    body = {"full_name": "Real Helper", "email": "real@example.test", "status": "active"}
    for role in ["planner", "driver", "helper", "sales"]:
        uid = t.make_user(env, name=role, email=f"{role}@example.test", role=role)
        assert env.client.patch(url, headers=t.bearer(uid, role), json=body).status_code == 403
    assert env.client.patch(url, headers=env.headers, json={**body, "email": "ADMIN@example.test"}).status_code == 409
    assert env.client.patch(url, headers=env.headers, json={**body, "phone": "bad"}).status_code == 400
    assert env.client.patch(f"/api/admin/users/{env.admin}", headers=env.headers, json={**body, "status": "disabled"}).status_code == 400
    with env.Session() as db:
        assert db.get(User, env.helper).email == "fake@example.test"
