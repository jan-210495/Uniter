"""v1.0.4 slice 2: account management and admin-only settings.

Accounts are driven through the real sign-in, password-change and admin
flows over HTTP against a temporary database built by the source seed path
(fixtures shared with ``test_auth``). Generated temporary passwords are
secrets: assertions about them never put the value in a failure message.
"""
import asyncio
import logging
import math
import re
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import auth, users
from backend.database import User, UserSession
from backend.main import app
from backend.test_auth import (  # noqa: F401 - shared fixtures
    CSRF_META, NEW_PASSWORD, change_password, complete_bootstrap, env, error_text, fresh,
    login, page_token, session_cookie,
)

SELLER_PASSWORD = "Seller-Pass-77"
PROTECTED_SETTINGS = (
    ("/api/settings/timezone", {"timezone": "UTC"}, "timezone", "UTC"),
    ("/api/settings/mtn-domain", {"domain": "11111"}, "mtn_domain", "11111"),
    ("/api/settings/syriatel-domain", {"domain": "22222"}, "syriatel_domain", "22222"),
    ("/api/settings/commission", {"rate": "0.07"}, "commission_rate", "0.07"),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def assert_secret_absent(secret: str, haystack: str | bytes, where: str) -> None:
    """Check a secret is absent without echoing it in the failure report."""
    needle = secret.encode() if isinstance(haystack, bytes) else secret
    leaked = needle in haystack
    assert not leaked, f"temporary password found in {where}"


def outcome(response) -> tuple[int, str | None]:
    """Status and redirect target only: safe to show in a failure report."""
    return response.status_code, response.headers.get("location")


def sign_in_with(client: TestClient, username: str, secret: str) -> tuple[int, str | None]:
    """Sign in with a secret without letting pytest echo it on failure:
    callers assign the result before asserting on it."""
    return outcome(login(client, username, secret))


def replace_secret(client: TestClient, current: str, new: str) -> tuple[int, str | None]:
    return outcome(change_password(client, current, new))


def differ(first: str, second: str) -> bool:
    return first != second


def browser(client: TestClient) -> TestClient:
    """Attach the session's CSRF token, as static/csrf.js does for fetch()."""
    client.headers[auth.CSRF_HEADER] = page_token(client)
    return client


def create_seller(admin: TestClient, username: str = "sara") -> tuple[dict, str]:
    response = admin.post("/api/users", json={"username": username})
    assert response.status_code == 201, response.status_code
    body = response.json()
    return body["user"], body.pop("temporary_password")


def first_seller_login(client: TestClient, username: str, temporary: str) -> None:
    signed_in = sign_in_with(client, username, temporary)
    assert signed_in == (303, "/change-password")
    changed = replace_secret(client, temporary, SELLER_PASSWORD)
    assert changed == (303, "/pos")


def user_row(env, username: str) -> User:
    async def read(session):
        return await session.scalar(select(User).where(User.username == username))
    return env.run(read)


def session_count(env, user_id: int) -> int:
    async def read(session):
        return len((await session.scalars(select(UserSession).where(UserSession.user_id == user_id))).all())
    return env.run(read)


@pytest.fixture
def admin(fresh) -> Iterator[TestClient]:
    with TestClient(app) as client:
        complete_bootstrap(client)
        yield browser(client)


@pytest.fixture
def seller_account(admin) -> tuple[dict, str]:
    return create_seller(admin)


@pytest.fixture
def seller(admin, seller_account) -> Iterator[TestClient]:
    user, temporary = seller_account
    with TestClient(app) as client:
        first_seller_login(client, user["username"], temporary)
        yield browser(client)


# ---------------------------------------------------------------------------
# 1. anonymous access
# ---------------------------------------------------------------------------

def test_anonymous_cannot_reach_user_management(fresh):
    with TestClient(app) as owner:  # an initialised system with its admin
        complete_bootstrap(owner)
    with TestClient(app) as anonymous:
        page = anonymous.get("/users", follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"].startswith("/login")
        assert anonymous.get("/api/users").status_code == 401
        for method, path, body in (
            ("POST", "/api/users", {"username": "intruder"}),
            ("PATCH", "/api/users/1", {"is_active": False}),
            ("POST", "/api/users/1/reset-password", None),
        ):
            kwargs = {"json": body} if body is not None else {}
            assert anonymous.request(method, path, **kwargs).status_code == 401
    assert [u.username for u in fresh.users()] == ["admin"]


# ---------------------------------------------------------------------------
# 2 + 9. sellers are refused on the server, not just in the UI
# ---------------------------------------------------------------------------

def test_seller_cannot_manage_accounts(seller, fresh, seller_account):
    seller_id = seller_account[0]["id"]
    admin_id = user_row(fresh, "admin").id
    page = seller.get("/users")
    assert page.status_code == 403
    assert "Only an admin" in page.text
    assert "يقتصر" in seller.get("/users", params={"lang": "ar"}).text
    assert seller.get("/api/users").status_code == 403

    refusals = []
    for target in (admin_id, seller_id, 999_999):  # existing or not: same answer
        refusals.append(seller.patch(f"/api/users/{target}", json={"is_active": False}))
        refusals.append(seller.patch(f"/api/users/{target}", json={"is_active": True}))
        refusals.append(seller.post(f"/api/users/{target}/reset-password"))
    refusals.append(seller.post("/api/users", json={"username": "second-seller"}))
    assert {r.status_code for r in refusals} == {403}
    assert {r.json()["detail"] for r in refusals} == {"Admin access required"}

    assert [u.username for u in fresh.users()] == ["admin", "sara"]
    assert all(u.is_active for u in fresh.users())
    assert auth.verify_password(NEW_PASSWORD, user_row(fresh, "admin").password_hash)


def test_seller_cannot_change_any_protected_setting(seller, admin):
    before = {key: admin.get(path).json()[key] for path, _, key, _ in PROTECTED_SETTINGS}
    for path, body, key, _ in PROTECTED_SETTINGS:
        response = seller.put(path, json=body)
        assert response.status_code == 403, path
        assert response.json() == {"detail": "Admin access required"}
        # Invalid bodies are refused for the role first, too.
        assert seller.put(path, json={}).status_code == 403
    after = {key: admin.get(path).json()[key] for path, _, key, _ in PROTECTED_SETTINGS}
    assert after == before


def test_admin_can_change_all_protected_settings(admin):
    for path, body, key, value in PROTECTED_SETTINGS:
        response = admin.put(path, json=body)
        assert response.status_code == 200, path
        assert admin.get(path).json()[key] == value


def test_admin_only_list_is_not_expanded(seller):
    """Currency denomination stays a shared setting, as before this slice."""
    response = seller.put("/api/currency/denomination", json={"scale_name": "post-2026"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 3. creation, normalisation and uniqueness
# ---------------------------------------------------------------------------

def test_admin_creates_seller_with_normalised_unique_username(admin, fresh):
    response = admin.post("/api/users", json={"username": "  Sara.K "})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"user", "temporary_password"}
    user = body["user"]
    assert user["username"] == "sara.k"
    assert user["role"] == "seller"
    assert user["is_active"] is True
    assert user["must_change_password"] is True
    assert user["created_at"]
    assert set(user) == {"id", "username", "role", "is_active", "must_change_password", "created_at"}

    for duplicate in ("SARA.K", " sara.k\t", "ｓａｒａ.ｋ", "admin", " ADMIN "):
        clash = admin.post("/api/users", json={"username": duplicate})
        assert clash.status_code == 409, duplicate
        issued = "temporary_password" in clash.text
        assert not issued, f"a refused create for {duplicate!r} issued a password"
    for invalid in ("", "   ", "ab", "has space", "x" * 65, "bad/char", "semi;colon"):
        assert admin.post("/api/users", json={"username": invalid}).status_code == 422, invalid
    # No additional roles and no way to mint another admin.
    for extra in ({"role": "admin"}, {"role": "seller"}, {"role": "manager"}, {"password": "chosen"}):
        assert admin.post("/api/users", json={"username": "extra", **extra}).status_code == 422
    assert [u.username for u in fresh.users()] == ["admin", "sara.k"]
    assert {u.role for u in fresh.users()} == {"admin", "seller"}


def test_seller_signs_in_with_any_case_or_spacing_of_username(admin):
    _, temporary = create_seller(admin, "Omar")
    with TestClient(app) as client:
        signed_in = sign_in_with(client, "  OMAR ", temporary)
        assert signed_in == (303, "/change-password")


def test_users_list_shows_only_safe_fields(admin, seller_account):
    listing = admin.get("/api/users")
    assert listing.status_code == 200
    rows = listing.json()
    assert [(r["username"], r["role"], r["is_active"]) for r in rows] == [
        ("admin", "admin", True), ("sara", "seller", True),
    ]
    for row in rows:
        assert set(row) == {"id", "username", "role", "is_active", "must_change_password", "created_at"}
    assert "scrypt$" not in listing.text and "password_hash" not in listing.text


# ---------------------------------------------------------------------------
# 4. temporary password: once, works, forces change
# ---------------------------------------------------------------------------

def test_temporary_passwords_are_high_entropy_and_unique():
    alphabet = set(users.TEMP_PASSWORD_ALPHABET)
    assert math.log2(len(alphabet)) * users.TEMP_PASSWORD_SYMBOLS >= 80
    generated = [users.generate_temporary_password() for _ in range(200)]
    assert len(set(generated)) == len(generated)
    for value in generated:
        symbols = value.replace("-", "")
        assert len(symbols) == users.TEMP_PASSWORD_SYMBOLS
        assert set(symbols) <= alphabet
        assert len(value) >= auth.PASSWORD_MIN_LENGTH


def test_temporary_password_is_shown_once_and_forces_change(admin, fresh):
    response = admin.post("/api/users", json={"username": "sara"})
    assert response.headers["cache-control"] == "no-store"
    temporary = response.json()["temporary_password"]
    assert_secret_absent(temporary, admin.get("/api/users").text, "users API")
    assert_secret_absent(temporary, admin.get("/users").text, "users page")
    assert_secret_absent(temporary, admin.get("/users", params={"lang": "ar"}).text, "users page (ar)")
    relisted = "temporary_password" in admin.get("/api/users").text
    assert not relisted, "the users list re-displays a temporary password"

    with TestClient(app) as client:
        signed_in = sign_in_with(client, "sara", temporary)
        assert signed_in == (303, "/change-password")
        assert client.get("/pos", follow_redirects=False).headers["location"].startswith("/change-password")
        assert client.get("/api/customers").status_code == 403
        assert client.get("/users", follow_redirects=False).headers["location"].startswith("/change-password")
        token = CSRF_META.search(client.get("/change-password").text)
        assert token is None  # the forced-change page has no app shell
        assert client.get("/api/users").status_code == 403
        changed = replace_secret(client, temporary, SELLER_PASSWORD)
        assert changed == (303, "/pos")
        assert client.get("/pos").status_code == 200
        client.cookies.clear()
        retired = sign_in_with(client, "sara", temporary)
        assert retired[0] == 401
        assert login(client, "sara", SELLER_PASSWORD).headers["location"] == "/pos"
    assert user_row(fresh, "sara").must_change_password is False


# ---------------------------------------------------------------------------
# 5. plaintext never persisted or logged
# ---------------------------------------------------------------------------

def _database_text(env) -> tuple[str, bytes]:
    path = env.engine.url.database
    connection = sqlite3.connect(path)
    try:
        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    with open(path, "rb") as handle:
        raw = handle.read()
    return dump, raw


def test_plaintext_temporary_passwords_are_never_persisted_or_logged(admin, fresh, caplog):
    caplog.set_level(logging.DEBUG)
    user, created = create_seller(admin)
    reset = admin.post(f"/api/users/{user['id']}/reset-password").json()["temporary_password"]
    row = user_row(fresh, "sara")
    assert row.password_hash.startswith("scrypt$")
    reset_matches = auth.verify_password(reset, row.password_hash)
    created_matches = auth.verify_password(created, row.password_hash)
    assert reset_matches and not created_matches

    dump, raw = _database_text(fresh)
    for secret in (created, reset):
        assert_secret_absent(secret, dump, "database dump")
        assert_secret_absent(secret, raw, "database file")
        assert_secret_absent(secret, caplog.text, "logs")
    assert "temporary_password" not in dump


# ---------------------------------------------------------------------------
# 6. admin reset
# ---------------------------------------------------------------------------

def test_admin_reset_issues_new_one_time_password(admin, seller, seller_account, fresh):
    user, original = seller_account
    assert seller.get("/pos").status_code == 200
    response = admin.post(f"/api/users/{user['id']}/reset-password")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    reset = body["temporary_password"]
    is_new = differ(reset, original)
    assert is_new, "reset must issue a new temporary password"
    assert body["user"]["must_change_password"] is True

    # The seller's live session ends at once.
    assert seller.get("/pos", follow_redirects=False).headers["location"].startswith("/login")
    assert session_count(fresh, user["id"]) == 0
    with TestClient(app) as client:
        assert login(client, "sara", SELLER_PASSWORD).status_code == 401
        replaced = sign_in_with(client, "sara", original)
        assert replaced[0] == 401
        signed_in = sign_in_with(client, "sara", reset)
        assert signed_in == (303, "/change-password")
        assert client.get("/pos", follow_redirects=False).headers["location"].startswith("/change-password")
        changed = replace_secret(client, reset, "Brand-New-Pass-5")
        assert changed == (303, "/pos")

    second = admin.post(f"/api/users/{user['id']}/reset-password").json()["temporary_password"]
    is_new = differ(second, reset)
    assert is_new, "every reset must issue a new temporary password"


def test_reset_clears_the_sellers_sign_in_lockout(admin, seller_account):
    user, temporary = seller_account
    with TestClient(app) as client:
        for _ in range(auth.MAX_FAILURES_PER_USERNAME):
            login(client, "sara", "wrong-password")
        locked = sign_in_with(client, "sara", temporary)
        assert locked[0] == 429  # locked out
        reset = admin.post(f"/api/users/{user['id']}/reset-password").json()["temporary_password"]
        signed_in = sign_in_with(client, "sara", reset)
        assert signed_in == (303, "/change-password")


def test_reset_is_for_seller_accounts_only(admin, fresh):
    admin_id = user_row(fresh, "admin").id
    response = admin.post(f"/api/users/{admin_id}/reset-password")
    assert response.status_code == 409
    issued = "temporary_password" in response.text
    assert not issued, "a refused reset issued a password"
    assert auth.verify_password(NEW_PASSWORD, user_row(fresh, "admin").password_hash)
    assert admin.post("/api/users/999999/reset-password").status_code == 404


def test_users_change_only_their_own_password(seller, fresh):
    """The change-password form acts on the session's account only."""
    page = seller.get("/change-password")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = seller.post("/change-password", data={
        "current_password": SELLER_PASSWORD, "new_password": "Seller-Next-88",
        "confirm_password": "Seller-Next-88", "csrf_token": csrf,
        "username": "admin", "user_id": str(user_row(fresh, "admin").id),
    }, follow_redirects=False)
    assert response.status_code == 303
    assert auth.verify_password("Seller-Next-88", user_row(fresh, "sara").password_hash)
    assert auth.verify_password(NEW_PASSWORD, user_row(fresh, "admin").password_hash)


def test_accounts_are_never_physically_deleted(admin, seller_account, fresh):
    user, _ = seller_account
    assert admin.delete(f"/api/users/{user['id']}").status_code == 405
    assert user_row(fresh, "sara") is not None


# ---------------------------------------------------------------------------
# 7. deactivation and the reactivation policy
# ---------------------------------------------------------------------------

def test_deactivated_seller_sessions_are_rejected_immediately(admin, seller, seller_account, fresh):
    user, _ = seller_account
    assert seller.get("/api/customers").status_code == 200
    response = admin.patch(f"/api/users/{user['id']}", json={"is_active": False})
    assert response.status_code == 200 and response.json()["is_active"] is False
    assert session_count(fresh, user["id"]) == 0

    assert seller.get("/pos", follow_redirects=False).headers["location"].startswith("/login")
    assert seller.get("/api/customers").status_code == 401
    assert seller.post("/api/customers", json={"phone": "0933123456", "name": "X"}).status_code == 401

    with TestClient(app) as client:
        refused = login(client, "sara", SELLER_PASSWORD)
        unknown = login(client, "nobody-at-all", SELLER_PASSWORD)
        assert refused.status_code == unknown.status_code == 401
        assert error_text(refused) == error_text(unknown)  # no hint the account exists


def test_session_check_alone_rejects_inactive_accounts(seller, seller_account, fresh):
    """Independent of the session purge in the admin action: the slice-1
    session loader refuses a live session whose account became inactive."""
    user, _ = seller_account
    assert session_count(fresh, user["id"]) == 1

    async def deactivate_directly(session):
        account = await session.get(User, user["id"])
        account.is_active = False
        await session.commit()
    fresh.run(deactivate_directly)
    assert session_count(fresh, user["id"]) == 1  # the row is still there
    assert seller.get("/api/customers").status_code == 401
    assert seller.get("/pos", follow_redirects=False).headers["location"].startswith("/login")
    assert session_count(fresh, user["id"]) == 0  # and is removed on use


def test_reactivation_keeps_password_but_requires_a_new_sign_in(admin, seller, seller_account, fresh):
    """Policy: reactivating restores the existing password and its forced-change
    state; sessions revoked at deactivation stay revoked."""
    user, _ = seller_account
    stale = session_cookie(seller)
    admin.patch(f"/api/users/{user['id']}", json={"is_active": False})
    response = admin.patch(f"/api/users/{user['id']}", json={"is_active": True})
    assert response.status_code == 200 and response.json()["is_active"] is True
    with TestClient(app) as client:
        client.cookies.set(auth.SESSION_COOKIE, stale)
        assert client.get("/pos", follow_redirects=False).headers["location"].startswith("/login")
        client.cookies.clear()
        assert login(client, "sara", SELLER_PASSWORD).headers["location"] == "/pos"
    assert user_row(fresh, "sara").must_change_password is False


def test_pending_change_survives_deactivation(admin, fresh):
    user, temporary = create_seller(admin, "nour")
    admin.patch(f"/api/users/{user['id']}", json={"is_active": False})
    admin.patch(f"/api/users/{user['id']}", json={"is_active": True})
    with TestClient(app) as client:
        signed_in = sign_in_with(client, "nour", temporary)
        assert signed_in == (303, "/change-password")


def test_toggle_validation(admin, seller_account):
    user, _ = seller_account
    for body in ({}, {"is_active": "nope"}, {"is_active": True, "username": "renamed"}):
        assert admin.patch(f"/api/users/{user['id']}", json=body).status_code == 422
    assert admin.patch("/api/users/999999", json={"is_active": False}).status_code == 404


# ---------------------------------------------------------------------------
# 8. the last active admin always remains
# ---------------------------------------------------------------------------

def test_admin_cannot_deactivate_or_demote_themselves(admin, fresh):
    admin_id = user_row(fresh, "admin").id
    response = admin.patch(f"/api/users/{admin_id}", json={"is_active": False})
    assert response.status_code == 409
    assert admin.patch(f"/api/users/{admin_id}", json={"role": "seller"}).status_code == 422
    row = user_row(fresh, "admin")
    assert row.is_active is True and row.role == "admin"
    assert admin.get("/pos").status_code == 200


def test_last_active_admin_guard(admin, fresh):
    """With a second admin (created directly; no UI makes admins) the guard
    allows deactivating one admin but never the final active one."""
    async def add_admin(session):
        session.add(User(username="backup-admin", password_hash=auth.hash_password("Backup-Pass-1"),
                         role="admin", must_change_password=False))
        await session.commit()
    fresh.run(add_admin)
    backup = user_row(fresh, "backup-admin")
    primary = user_row(fresh, "admin")

    async def check(user_id):
        async with fresh.sessions() as session:
            return await users.would_remove_last_admin(session, await session.get(User, user_id))

    assert asyncio.run(check(backup.id)) is False
    assert admin.patch(f"/api/users/{backup.id}", json={"is_active": False}).status_code == 200
    assert asyncio.run(check(primary.id)) is True
    # Even another admin's request could not remove the final active admin.
    async def deactivate_primary(session):
        user = await session.get(User, primary.id)
        return await users.would_remove_last_admin(session, user)
    assert fresh.run(deactivate_primary) is True
    assert admin.patch(f"/api/users/{primary.id}", json={"is_active": False}).status_code == 409
    assert user_row(fresh, "admin").is_active is True


# ---------------------------------------------------------------------------
# 10. both roles use the normal app and can view settings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["admin", "seller"])
def test_both_roles_view_settings_and_business_data(role, request):
    client = request.getfixturevalue(role)
    for path in ("/pos", "/topup", "/transactions", "/customers", "/settlement", "/stats",
                 "/settings", "/operators/mtn", "/settings?lang=ar"):
        assert client.get(path).status_code == 200, path
    for path, _, key, _ in PROTECTED_SETTINGS:
        assert key in client.get(path).json()
    for path in ("/api/customers", "/api/transactions", "/api/operators/balance", "/api/stats/summary",
                 "/api/currency/denomination", "/api/search?q=09"):
        assert client.get(path).status_code == 200, path
    phone = "0933123456" if role == "admin" else "0944123456"
    assert client.post("/api/customers", json={"phone": phone, "name": role}).status_code == 200
    assert client.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 1000}).status_code == 200


FIELDSETS = ("commission-fieldset", "timezone-fieldset", "mtn-domain-fieldset", "syriatel-domain-fieldset")


def _fieldset_tag(html: str, fieldset_id: str) -> str:
    match = re.search(rf'<fieldset[^>]*\bid="{fieldset_id}"[^>]*>', html)
    assert match, fieldset_id
    return match.group(0)


def test_settings_controls_are_read_only_for_sellers(seller):
    for lang, note in (("en", "Only an admin can change this setting."), ("ar", "يمكن للمدير فقط تغيير هذا الإعداد.")):
        html = seller.get("/settings", params={"lang": lang}).text
        for fieldset in FIELDSETS:
            assert re.search(r"\bdisabled\b", _fieldset_tag(html, fieldset)), fieldset
        assert html.count('class="admin-only-note') == 4
        assert note in html
        # The shared denomination control stays editable.
        assert 'id="denom-select"' in html
        assert not re.search(r'<fieldset[^>]*\bid="denomination-fieldset"[^>]*\bdisabled\b', html)


def test_settings_controls_are_editable_for_admins(admin):
    html = admin.get("/settings").text
    for fieldset in FIELDSETS:
        assert not re.search(r"\bdisabled\b", _fieldset_tag(html, fieldset)), fieldset
    assert "admin-only-note" not in html


# ---------------------------------------------------------------------------
# 11. CSRF on every unsafe account action
# ---------------------------------------------------------------------------

def test_account_actions_require_csrf(admin, seller_account, fresh):
    user, _ = seller_account
    token = admin.headers.pop(auth.CSRF_HEADER)
    actions = (
        ("POST", "/api/users", {"username": "csrf-victim"}),
        ("PATCH", f"/api/users/{user['id']}", {"is_active": False}),
        ("POST", f"/api/users/{user['id']}/reset-password", None),
    )
    for method, path, body in actions:
        kwargs = {"json": body} if body is not None else {}
        assert admin.request(method, path, **kwargs).status_code == 403, path
        assert admin.request(method, path, headers={auth.CSRF_HEADER: "forged"}, **kwargs).status_code == 403
        cross = admin.request(method, path, headers={auth.CSRF_HEADER: token, "Origin": "http://evil.example"}, **kwargs)
        assert cross.status_code == 403
    assert [u.username for u in fresh.users()] == ["admin", "sara"]
    assert user_row(fresh, "sara").is_active is True

    headers = {auth.CSRF_HEADER: token}
    assert admin.post("/api/users", json={"username": "csrf-ok"}, headers=headers).status_code == 201
    assert admin.patch(f"/api/users/{user['id']}", json={"is_active": False}, headers=headers).status_code == 200
    assert admin.post(f"/api/users/{user['id']}/reset-password", headers=headers).status_code == 200


def test_protected_settings_require_csrf_for_admins(admin):
    admin.headers.pop(auth.CSRF_HEADER)
    for path, body, _, _ in PROTECTED_SETTINGS:
        assert admin.put(path, json=body).status_code == 403, path


# ---------------------------------------------------------------------------
# 12. bilingual page and navigation
# ---------------------------------------------------------------------------

def _drawer_user_links(html: str) -> list[str]:
    drawer = html[html.index('id="app-drawer"'):html.index("</nav>", html.index('id="app-drawer"'))]
    return re.findall(r'<a href="/users[^"]*"[^>]*>\s*([^<]+?)\s*</a>', drawer)


def test_navigation_reaches_user_management_for_admins(admin, seller):
    assert _drawer_user_links(admin.get("/pos").text) == ["Users"]
    assert _drawer_user_links(admin.get("/pos", params={"lang": "ar"}).text) == ["المستخدمون"]
    for lang in ("en", "ar"):
        assert _drawer_user_links(seller.get("/pos", params={"lang": lang}).text) == []
    current = admin.get("/users").text
    assert re.search(r'<a href="/users"[^>]*aria-current="page"', current)


def test_users_page_renders_in_both_languages(admin, seller_account):
    english = admin.get("/users")
    arabic = admin.get("/users", params={"lang": "ar"})
    assert english.status_code == arabic.status_code == 200
    assert '<html lang="en" dir="ltr">' in english.text
    assert '<html lang="ar" dir="rtl">' in arabic.text
    for text in ("Users", "Username", "Role", "Status", "Created", "Seller", "Admin", "Active",
                 "Create seller", "Reset password", "Deactivate"):
        assert text in english.text, text
    for text in ("المستخدمون", "اسم المستخدم", "الدور", "الحالة", "تاريخ الإنشاء", "بائع", "مدير",
                 "نشط", "إنشاء حساب بائع", "إعادة تعيين كلمة المرور", "تعطيل"):
        assert text in arabic.text, text
    for html in (english.text, arabic.text):
        assert "sara" in html
        assert "scrypt$" not in html
        assert 'id="temporary-password-panel"' in html


# ---------------------------------------------------------------------------
# rendered page script against the real API (one-time display behaviour)
# ---------------------------------------------------------------------------

def _run_users_script(admin: TestClient) -> tuple[dict, list[dict]]:
    import json
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("Node.js required for rendered script execution")
    html = admin.get("/users").text
    payload = {"elements": re.findall(r'\bid="([^"]+)"', html),
               "script": re.findall(r"<script>(.*?)</script>", html, re.S)[0]}
    requests = []
    with subprocess.Popen(["node", str(Path(__file__).with_name("users_dom_harness.cjs"))],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) as process:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        while True:
            line = process.stdout.readline()
            assert line, "harness stopped: " + process.stderr.read()[:2000]
            message = json.loads(line)
            if "result" in message:
                result = message["result"]
                break
            request = message["request"]
            requests.append({"method": request["method"], "url": request["url"]})
            kwargs = {"json": request["body"]} if request["body"] is not None else {}
            response = admin.request(request["method"], request["url"], **kwargs)
            process.stdin.write(json.dumps({"status": response.status_code, "body": response.json()}) + "\n")
            process.stdin.flush()
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    return result, requests


def test_users_page_script_shows_each_temporary_password_once(admin, fresh):
    result, requests = _run_users_script(admin)
    # Pull the secrets out first so no assertion below can print them.
    created_password = result["created"].pop("password")
    reset_password = result["reset"].pop("password")
    for stage in ("dismissed", "invalid", "left"):
        still_shown = result[stage].pop("password") != ""
        assert not still_shown, f"temporary password still displayed after '{stage}'"

    assert [(r["method"], r["url"]) for r in requests] == [
        ("POST", "/api/users"), ("GET", "/api/users"),
        ("POST", f"/api/users/{user_row(fresh, 'lina').id}/reset-password"), ("GET", "/api/users"),
    ]  # the invalid username never reached the server
    created = result["created"]
    assert created["panelHidden"] is False and created["panelUser"] == "lina"
    assert created["createFeedback"] == "Account created."
    assert [row["text"][0] for row in created["rows"]] == ["admin", "lina"]
    assert created["rows"][0]["actions"] == []  # own admin row: no self-deactivate, no reset
    assert created["rows"][1]["actions"] == ["reset", "deactivate"]
    assert "Password change pending" in created["rows"][1]["text"]
    assert result["dismissed"]["panelHidden"] is True and result["dismissed"]["panelUser"] == ""
    assert result["invalid"]["createFeedback"].startswith("Invalid username")
    assert result["reset"]["panelHidden"] is False and result["reset"]["feedback"] == "Password reset."
    assert result["left"]["panelHidden"] is True  # cleared when leaving the page

    is_new = differ(created_password, reset_password)
    assert is_new, "reset must issue a new temporary password"
    with TestClient(app) as client:
        replaced = sign_in_with(client, "lina", created_password)
        assert replaced[0] == 401
        signed_in = sign_in_with(client, "lina", reset_password)
        assert signed_in == (303, "/change-password")
