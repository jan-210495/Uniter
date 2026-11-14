"""Authentication foundation: bootstrap, sessions, CSRF and throttling.

Every scenario drives the real entry points (``seed.main``/``init_db``, the
desktop seed and the web startup upgrade) against a temporary database, then
talks to the app over HTTP exactly as a browser would.
"""
import asyncio
import logging
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from backend import auth, database, seed
from backend.database import LoginAttempt, User, UserSession, get_db
from backend.main import app

NEW_PASSWORD = "Correct-Horse-42"
CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')
CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
LOGIN_ERROR = re.compile(r'<p id="login-error"[^>]*>(.*?)</p>', re.S)


@dataclass
class Env:
    engine: AsyncEngine
    sessions: async_sessionmaker

    def run(self, factory):
        async def runner():
            async with self.sessions() as session:
                return await factory(session)
        return asyncio.run(runner())

    def users(self) -> list[User]:
        async def read(session):
            return (await session.scalars(select(User).order_by(User.id))).all()
        return self.run(read)

    def count(self, model) -> int:
        async def read(session):
            return await session.scalar(select(func.count()).select_from(model))
        return self.run(read)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Iterator[Env]:
    from backend import desktop

    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "auth.db")))
    event.listen(engine.sync_engine, "connect", database.enable_sqlite_foreign_keys)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "DATABASE_URL", engine.url)
    monkeypatch.setattr(seed, "SessionLocal", sessions)
    monkeypatch.setattr(desktop, "engine", engine)
    monkeypatch.setattr(desktop, "SessionLocal", sessions)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        yield Env(engine, sessions)
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


@pytest.fixture
def fresh(env) -> Env:
    """A database created by the source-mode seeding entry point."""
    asyncio.run(seed.main())
    return env


@pytest.fixture
def client(fresh) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def form_token(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return CSRF_FIELD.search(response.text).group(1)


def page_token(client: TestClient, path: str = "/pos") -> str:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return CSRF_META.search(response.text).group(1)


def login(client: TestClient, username: str, password: str, **extra) -> object:
    token = form_token(client, "/login")
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token, **extra},
        follow_redirects=False,
    )


def change_password(client: TestClient, current: str, new: str, confirm: str | None = None):
    token = form_token(client, "/change-password")
    return client.post(
        "/change-password",
        data={
            "current_password": current,
            "new_password": new,
            "confirm_password": new if confirm is None else confirm,
            "csrf_token": token,
        },
        follow_redirects=False,
    )


def logout(client: TestClient, token: str):
    return client.post("/logout", data={"csrf_token": token}, follow_redirects=False)


def complete_bootstrap(client: TestClient) -> None:
    assert login(client, "admin", "admin").headers["location"] == "/change-password"
    response = change_password(client, "admin", NEW_PASSWORD)
    assert response.status_code == 303 and response.headers["location"] == "/pos"


def session_cookie(client: TestClient) -> str | None:
    return client.cookies.get(auth.SESSION_COOKIE)


def with_cookie(client: TestClient, value: str, path: str, method: str = "GET", **kwargs):
    client.cookies.clear()
    headers = {"Cookie": f"{auth.SESSION_COOKIE}={value}", **kwargs.pop("headers", {})}
    return client.request(method, path, headers=headers, follow_redirects=False, **kwargs)


def error_text(response) -> str:
    match = LOGIN_ERROR.search(response.text)
    assert match, response.text
    return " ".join(match.group(1).split())


# ---------------------------------------------------------------------------
# 1. fresh initialisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["source", "desktop"])
def test_fresh_initialisation_creates_exactly_one_bootstrap_admin(env, entry):
    from backend import desktop

    if entry == "source":
        asyncio.run(seed.main())
    else:
        asyncio.run(desktop._seed_database())

    users = env.users()
    assert len(users) == 1
    admin = users[0]
    assert admin.username == "admin"
    assert admin.role == "admin"
    assert admin.is_active is True
    assert admin.must_change_password is True
    # Hashed, salted and never the plaintext.
    assert admin.password_hash != "admin"
    assert "admin" not in admin.password_hash
    assert admin.password_hash.startswith("scrypt$")
    assert auth.verify_password("admin", admin.password_hash)
    assert not any(user.role == "seller" for user in users)
    assert env.count(UserSession) == 0


def test_only_admin_and_seller_roles_are_storable(fresh):
    async def insert(session, role):
        session.add(User(username=f"user-{role}", password_hash=auth.hash_password("x" * 12), role=role))
        await session.commit()

    fresh.run(lambda session: insert(session, "seller"))
    with pytest.raises(IntegrityError):
        fresh.run(lambda session: insert(session, "operator"))
    assert sorted(database.USER_ROLES) == ["admin", "seller"]


def test_password_hashes_are_salted_and_verified_in_constant_form():
    first, second = auth.hash_password("same password"), auth.hash_password("same password")
    assert first != second
    assert auth.verify_password("same password", first)
    assert not auth.verify_password("other password", first)
    assert not auth.verify_password("same password", "not-a-hash")
    assert not auth.verify_password("same password", "")


# ---------------------------------------------------------------------------
# 2 + 7. bootstrap login and the forced password change
# ---------------------------------------------------------------------------

def test_bootstrap_login_forces_an_immediate_password_change(client):
    response = login(client, "admin", "admin")
    assert response.status_code == 303
    assert response.headers["location"] == "/change-password"
    assert session_cookie(client)

    page = client.get("/change-password")
    assert page.status_code == 200
    assert 'name="new_password"' in page.text
    token = CSRF_FIELD.search(page.text).group(1)

    for path in ("/pos", "/settings", "/customers", "/operators/mtn", "/pos?lang=ar"):
        blocked = client.get(path, follow_redirects=False)
        assert blocked.status_code == 303
        assert blocked.headers["location"].startswith("/change-password")
    for path in ("/api/customers", "/api/transactions", "/api/settings/commission", "/api/search?q=1"):
        blocked = client.get(path)
        assert blocked.status_code == 403
        assert blocked.json() == {"detail": "Password change required"}
    # Even with a valid CSRF token, state-changing APIs stay closed.
    for method, path, body in (
        ("POST", "/api/customers", {"phone": "0933123456", "name": "Bypass"}),
        ("PUT", "/api/settings/timezone", {"timezone": "UTC"}),
        ("POST", "/api/operators/topup", {"operator_code": "mtn", "amount_cents": 100}),
    ):
        blocked = client.request(method, path, json=body, headers={auth.CSRF_HEADER: token})
        assert blocked.status_code == 403
    assert client.get("/api/health").status_code == 200


def test_forced_change_rejects_weak_or_unchanged_passwords(client, fresh):
    login(client, "admin", "admin")
    for current, new, confirm in (
        ("wrong-password", NEW_PASSWORD, NEW_PASSWORD),  # current must be proven
        ("admin", "admin", "admin"),                    # bootstrap value cannot be kept
        ("admin", "short", "short"),                    # minimum length
        ("admin", NEW_PASSWORD, NEW_PASSWORD + "x"),    # confirmation mismatch
    ):
        response = change_password(client, current, new, confirm)
        assert response.status_code == 400
        assert client.get("/pos", follow_redirects=False).headers["location"].startswith("/change-password")
    assert fresh.users()[0].must_change_password is True
    assert auth.verify_password("admin", fresh.users()[0].password_hash)


# ---------------------------------------------------------------------------
# 3. the bootstrap password is retired once changed
# ---------------------------------------------------------------------------

def test_changing_password_retires_bootstrap_credential(client, fresh):
    login(client, "admin", "admin")
    before_change = session_cookie(client)
    response = change_password(client, "admin", NEW_PASSWORD)
    assert response.status_code == 303 and response.headers["location"] == "/pos"
    after_change = session_cookie(client)
    assert after_change and after_change != before_change  # rotated, no fixation

    assert client.get("/pos").status_code == 200
    assert client.get("/api/customers").status_code == 200
    admin = fresh.users()[0]
    assert admin.must_change_password is False
    assert not auth.verify_password("admin", admin.password_hash)

    # The pre-change session id is dead server-side.
    assert with_cookie(client, before_change, "/pos").status_code == 303

    client.cookies.clear()
    failed = login(client, "admin", "admin")
    assert failed.status_code == 401
    assert session_cookie(client) is None
    ok = login(client, "admin", NEW_PASSWORD)
    assert ok.status_code == 303 and ok.headers["location"] == "/pos"
    assert client.get("/pos").status_code == 200


def test_password_change_signs_out_other_sessions(fresh):
    with TestClient(app) as first, TestClient(app) as second:
        login(first, "admin", "admin")
        login(second, "admin", "admin")
        change_password(first, "admin", NEW_PASSWORD)
        assert first.get("/pos").status_code == 200
        assert second.get("/pos", follow_redirects=False).headers["location"].startswith("/login")


def test_login_redirects_only_to_local_paths(client):
    complete_bootstrap(client)
    client.cookies.clear()
    local = login(client, "admin", NEW_PASSWORD, next="/settings?lang=ar")
    assert local.headers["location"] == "/settings?lang=ar"
    for hostile in ("//evil.example/", "https://evil.example/", "/\\evil.example", "javascript:alert(1)"):
        client.cookies.clear()
        response = login(client, "admin", NEW_PASSWORD, next=hostile)
        assert response.headers["location"] == "/pos"


# ---------------------------------------------------------------------------
# 4. later startups never recreate or reset the bootstrap account
# ---------------------------------------------------------------------------

def test_later_startups_do_not_recreate_or_reset_bootstrap(client, fresh):
    from backend import desktop

    complete_bootstrap(client)
    changed_hash = fresh.users()[0].password_hash

    asyncio.run(seed.main())
    asyncio.run(desktop._seed_database())
    asyncio.run(database.upgrade_existing_database())
    with TestClient(app):  # the web startup hook
        pass

    users = fresh.users()
    assert len(users) == 1
    assert users[0].password_hash == changed_hash
    assert users[0].must_change_password is False
    client.cookies.clear()
    assert login(client, "admin", "admin").status_code == 401
    assert login(client, "admin", NEW_PASSWORD).status_code == 303


def test_bootstrap_is_never_recreated_even_if_users_disappear(fresh):
    from backend import desktop

    async def wipe(session):
        await session.execute(database.User.__table__.delete())
        await session.commit()

    fresh.run(wipe)
    asyncio.run(seed.main())
    asyncio.run(desktop._seed_database())
    asyncio.run(database.upgrade_existing_database())
    assert fresh.users() == []


def test_existing_pre_auth_database_gets_bootstrap_on_upgrade(env):
    """A v1.0.3 database (no auth tables) gains the one-time admin on startup."""
    async def legacy():
        async with env.engine.begin() as connection:
            await connection.run_sync(database.Base.metadata.create_all)
            for table in ("user_sessions", "login_attempts", "users"):
                await connection.exec_driver_sql(f"DROP TABLE {table}")
    asyncio.run(legacy())
    asyncio.run(database.upgrade_existing_database())
    users = env.users()
    assert [(u.username, u.role, u.must_change_password) for u in users] == [("admin", "admin", True)]
    asyncio.run(database.upgrade_existing_database())
    assert len(env.users()) == 1


def test_uninitialised_databases_are_not_bootstrapped(env, tmp_path):
    assert asyncio.run(auth.migrate_auth_schema(env.engine)) is False

    async def tables():
        async with env.engine.connect() as connection:
            return (await connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )).all()
    assert asyncio.run(tables()) == []


# ---------------------------------------------------------------------------
# 5. logout invalidates the server-side session
# ---------------------------------------------------------------------------

def test_logout_invalidates_server_side_session(client, fresh):
    complete_bootstrap(client)
    token = page_token(client)
    cookie = session_cookie(client)
    assert fresh.count(UserSession) == 1

    response = logout(client, token)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert fresh.count(UserSession) == 0
    assert session_cookie(client) is None
    # Replaying the old cookie no longer authenticates anything.
    assert with_cookie(client, cookie, "/pos").status_code == 303
    assert with_cookie(client, cookie, "/api/customers").status_code == 401


def test_logout_requires_csrf_token(client, fresh):
    complete_bootstrap(client)
    assert client.post("/logout", data={}, follow_redirects=False).status_code == 403
    assert client.post("/logout", data={"csrf_token": "forged"}, follow_redirects=False).status_code == 403
    assert client.get("/pos").status_code == 200
    assert fresh.count(UserSession) == 1


def test_forced_change_session_can_sign_out(client, fresh):
    login(client, "admin", "admin")
    token = form_token(client, "/change-password")
    assert logout(client, token).status_code == 303
    assert fresh.count(UserSession) == 0


def test_expired_and_idle_sessions_are_rejected(client, monkeypatch):
    complete_bootstrap(client)
    assert client.get("/pos").status_code == 200
    real_now = auth.utc_now
    monkeypatch.setattr(auth, "utc_now", lambda: real_now() + auth.SESSION_IDLE_TIMEOUT + timedelta(minutes=1))
    assert client.get("/pos", follow_redirects=False).status_code == 303
    assert client.get("/api/customers").status_code == 401


# ---------------------------------------------------------------------------
# 8. CSRF
# ---------------------------------------------------------------------------

def test_csrf_rejects_missing_or_invalid_tokens_and_accepts_valid(client):
    complete_bootstrap(client)
    token = page_token(client)
    body = {"phone": "0933123456", "name": "Customer"}

    assert client.post("/api/customers", json=body).status_code == 403
    assert client.post("/api/customers", json=body, headers={auth.CSRF_HEADER: ""}).status_code == 403
    assert client.post("/api/customers", json=body, headers={auth.CSRF_HEADER: "forged"}).status_code == 403
    assert client.post("/api/customers", json=body, headers={auth.CSRF_HEADER: token[:-1]}).status_code == 403
    # Rejected requests wrote nothing.
    assert client.get("/api/customers").json() == []

    hostile = client.post(
        "/api/customers", json=body,
        headers={auth.CSRF_HEADER: token, "Origin": "http://evil.example"},
    )
    assert hostile.status_code == 403

    accepted = client.post(
        "/api/customers", json=body,
        headers={auth.CSRF_HEADER: token, "Origin": "http://testserver"},
    )
    assert accepted.status_code == 200
    assert client.put(
        "/api/settings/timezone", json={"timezone": "UTC"}, headers={auth.CSRF_HEADER: token}
    ).status_code == 200
    # Safe methods never need the token.
    assert client.get("/api/customers").status_code == 200


def test_csrf_token_is_bound_to_its_session(fresh):
    with TestClient(app) as first, TestClient(app) as second:
        complete_bootstrap(first)
        login(second, "admin", NEW_PASSWORD)
        foreign = page_token(first)
        own = page_token(second)
        assert foreign != own
        body = {"phone": "0933123456", "name": "Customer"}
        assert second.post("/api/customers", json=body, headers={auth.CSRF_HEADER: foreign}).status_code == 403
        assert second.post("/api/customers", json=body, headers={auth.CSRF_HEADER: own}).status_code == 200


def test_csrf_rotates_with_the_session(client):
    login(client, "admin", "admin")
    before = form_token(client, "/change-password")
    change_password(client, "admin", NEW_PASSWORD)
    after = page_token(client)
    assert before != after
    body = {"phone": "0933123456", "name": "Customer"}
    assert client.post("/api/customers", json=body, headers={auth.CSRF_HEADER: before}).status_code == 403


def test_login_and_password_forms_require_csrf(client, fresh):
    client.get("/login")
    missing = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert missing.status_code == 403
    forged = client.post(
        "/login", data={"username": "admin", "password": "admin", "csrf_token": "forged"},
        follow_redirects=False,
    )
    assert forged.status_code == 403
    assert session_cookie(client) is None

    login(client, "admin", "admin")
    response = client.post(
        "/change-password",
        data={"current_password": "admin", "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert fresh.users()[0].must_change_password is True


def test_pages_expose_csrf_token_and_sign_out_control(client):
    complete_bootstrap(client)
    for lang, label in (("en", "Sign out"), ("ar", "تسجيل الخروج")):
        page = client.get("/pos", params={"lang": lang})
        token = CSRF_META.search(page.text).group(1)
        assert f'name="csrf_token" value="{token}"' in page.text
        assert 'action="/logout"' in page.text
        assert label in page.text
        assert 'src="/static/csrf.js"' in page.text


# ---------------------------------------------------------------------------
# 9. throttling without account-existence leaks
# ---------------------------------------------------------------------------

def test_login_errors_do_not_reveal_account_existence(client):
    complete_bootstrap(client)
    client.cookies.clear()
    wrong_password = login(client, "admin", "not-the-password")
    unknown_user = login(client, "nobody-here", "not-the-password")
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert error_text(wrong_password) == error_text(unknown_user)
    assert "admin" not in error_text(wrong_password).lower()


def test_username_throttling_locks_out_case_and_whitespace_variants(client, monkeypatch):
    complete_bootstrap(client)
    client.cookies.clear()
    variants = ["admin", "ADMIN", " Admin ", "aDmIn\t", "admin"]
    assert len(variants) == auth.MAX_FAILURES_PER_USERNAME
    for variant in variants:
        assert login(client, variant, "wrong-password").status_code == 401

    locked = login(client, "admin", NEW_PASSWORD)  # even the right password
    assert locked.status_code == 429
    assert session_cookie(client) is None
    assert login(client, "  ADMIN", NEW_PASSWORD).status_code == 429

    # Unknown usernames lock out identically, so lockout reveals nothing.
    for _ in range(auth.MAX_FAILURES_PER_USERNAME):
        login(client, "ghost", "wrong-password")
    ghost = login(client, "ghost", "wrong-password")
    assert ghost.status_code == 429
    assert error_text(ghost) == error_text(locked)

    # The lockout expires after the throttle window.
    real_now = auth.utc_now
    monkeypatch.setattr(auth, "utc_now", lambda: real_now() + auth.THROTTLE_WINDOW + timedelta(minutes=1))
    assert login(client, "admin", NEW_PASSWORD).status_code == 303


def test_successful_login_clears_username_failures(client):
    complete_bootstrap(client)
    client.cookies.clear()
    for _ in range(auth.MAX_FAILURES_PER_USERNAME - 1):
        assert login(client, "admin", "wrong-password").status_code == 401
    assert login(client, "admin", NEW_PASSWORD).status_code == 303
    client.cookies.clear()
    for _ in range(auth.MAX_FAILURES_PER_USERNAME - 1):
        assert login(client, "admin", "wrong-password").status_code == 401
    assert login(client, "admin", NEW_PASSWORD).status_code == 303


def test_address_throttling_stops_username_spraying(client, fresh):
    complete_bootstrap(client)
    client.cookies.clear()
    for index in range(auth.MAX_FAILURES_PER_ADDRESS):
        assert login(client, f"user-{index}", "wrong-password").status_code == 401
    assert login(client, "admin", NEW_PASSWORD).status_code == 429
    # Attempt keys are hashed: raw usernames never sit in the table.
    async def keys(session):
        return (await session.scalars(select(LoginAttempt.key_hash))).all()
    stored = fresh.run(keys)
    assert stored and not any("user-" in key for key in stored)


# ---------------------------------------------------------------------------
# cookies, secrets and logging hygiene
# ---------------------------------------------------------------------------

def _set_cookie(response, name: str) -> str:
    return next(h for h in response.headers.get_list("set-cookie") if h.startswith(f"{name}="))


def test_session_cookie_flags_over_http(client, fresh):
    response = login(client, "admin", "admin")
    header = _set_cookie(response, auth.SESSION_COOKIE).lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header
    assert "max-age=" in header
    assert "secure" not in header  # plain-HTTP local mode must still work
    # Only a hash of the session id is stored server-side.
    async def hashes(session):
        return (await session.scalars(select(UserSession.token_hash))).all()
    assert session_cookie(client) not in fresh.run(hashes)


def test_session_cookie_is_secure_over_https(fresh):
    with TestClient(app, base_url="https://testserver") as secure:
        response = login(secure, "admin", "admin")
        assert response.status_code == 303
        assert "secure" in _set_cookie(response, auth.SESSION_COOKIE).lower()
        assert secure.get("/change-password").status_code == 200


def test_secrets_never_reach_the_logs(client, caplog):
    caplog.set_level(logging.DEBUG)
    login(client, "admin", "admin")
    cookie = session_cookie(client)
    token = form_token(client, "/change-password")
    change_password(client, "admin", NEW_PASSWORD)
    client.cookies.clear()
    login(client, "admin", "Wrong-Secret-99")
    for secret in (NEW_PASSWORD, "Wrong-Secret-99", cookie, token):
        assert secret not in caplog.text


def test_no_hardcoded_signing_secret_or_default_password_in_auth_module():
    from pathlib import Path

    source = Path(auth.__file__).read_text()
    assert "SECRET_KEY" not in source
    assert not re.search(r"(?i)secret\s*=\s*['\"]", source)
