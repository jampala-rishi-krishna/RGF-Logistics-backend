"""Account messages preserve credentials and links inside the assignment email branding."""
from html import escape
from types import SimpleNamespace

import pytest

from routers.admin import _invite_email, _login_details_email


def account(role="helper"):
    return SimpleNamespace(full_name="Ana <Crew> & Co", email="ana@example.test",
                           phone=639171234567, role=role, staff_id=17)


def assert_branding(html):
    assert "RARECHAIN" in html
    assert "Rare Global Food Trading Corp." in html
    assert "mailto:martin@rareglobalfood.com" in html
    assert "#0B0B0B" in html and "#FAFAF8" in html
    assert html.count("<img ") == 2
    assert "photos/7464230/" in html and "photos/6169056/" in html
    assert "Ana &lt;Crew&gt; &amp; Co" in html
    assert "Ana <Crew>" not in html
    assert "IntelliFleet" not in html


@pytest.mark.parametrize("purpose,subject,action", [
    ("invite", "Your RareChain account invitation", "Set my password"),
    ("reset", "Reset your RareChain password", "Reset my password"),
])
@pytest.mark.parametrize("role", ["helper", "driver", "logistics_officer", "planner"])
def test_account_link_email_preserves_link_and_purpose(purpose, subject, action, role):
    link = "https://app.example.test/set-password?token=test-token&source=admin"
    actual_subject, html, text = _invite_email(account(role), link, purpose)
    assert actual_subject == subject
    assert_branding(html)
    assert f"href='{escape(link, quote=True)}'" in html
    assert action in html and "24 hours" in html and "once" in html
    assert link in text and "24 hours" in text
    assert role.replace("_", " ").title() in html
    assert "driver account" not in html


def test_login_details_escape_credentials_and_preserve_plain_text():
    password = 'Example<&"Password123'
    subject, html, text = _login_details_email(
        account(), password, {"title": "Delivery <Helper>", "warehouse": "A & B"})
    assert subject == "Your RareChain login details"
    assert_branding(html)
    assert escape(password) in html and password not in html
    assert password in text and "09171234567" in html and "09171234567" in text
    assert "Delivery &lt;Helper&gt;" in html and "A &amp; B" in html
    assert "ana@example.test" in html and "Staff ID" in html
    assert "change your password" in html and "change your password" in text


def test_login_details_work_without_optional_staff_fields():
    user = account("planner")
    user.full_name, user.phone, user.staff_id = None, None, None
    _, html, text = _login_details_email(user, "Example-password123", None)
    assert "Hi there," in html and "Hi there," in text
    assert "Staff ID" not in html and "Warehouse</td>" not in html
    assert "Planner" in html
