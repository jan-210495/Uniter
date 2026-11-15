"""v1.0.4 Slice 3.1: one-time first-run administrator setup.

There is no shared default credential any more. A database with no accounts
and no initialisation marker shows ``/setup``, where the first person to open
the app chooses the ``admin`` password. Setup is atomic (``BEGIN IMMEDIATE``),
one-time, and fails closed when an initialised database has no active admin.

Every scenario uses a disposable database under ``tmp_path``. Passwords used
here are test values; assertions never print them (see ``_contains``).
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend import auth, database, seed
from backend.database import LoginAttempt, Transaction, User, UserSession, get_db
from backend.main import app
from backend.test_auth import CSRF_FIELD, Env, env  # noqa: F401 - shared fixture

CHOSEN = "Shop-Owner-Pass-41"
OTHER = "Rival-Owner-Pass-93"
ROOT = Path(__file__).resolve().parent.parent
PROTECTED_PAGES = ("/pos", "/transactions", "/customers", "/settings", "/users", "/operators/mtn")
PROTECTED_APIS = ("/api/customers", "/api/transactions", "/api/users", "/api/settings/commission")
SETUP_ERROR = re.compile(r'<p id="setup-error"[^>]*>(.*?)</p>', re.S)


def _contains(haystack: str | bytes, needle: str) -> bool:
    """Membership test whose failure message never shows the secret."""
    if isinstance(haystack, bytes):
        return needle.encode() in haystack
    return needle in haystack


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def blank(env) -> Env:
    """A database initialised by the seeding entry point: no account yet."""
    asyncio.run(seed.main())
    return env


@pytest.fixture
def client(blank) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def users(env: Env) -> list[User]:
    async def read(session):
        return (await session.scalars(select(User).order_by(User.id))).all()
    return env.run(read)


def count(env: Env, model) -> int:
    async def read(session):
        return await session.scalar(select(func.count()).select_from(model))
    return env.run(read)


def markers(env: Env) -> set[str]:
    async def read(session):
        exists = await session.scalar(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ))
        if not exists:
            return set()
        return set((await session.execute(text("SELECT version FROM schema_migrations"))).scalars())
    return env.run(read)


def setup_token(client: TestClient, lang: str = "en") -> str:
    response = client.get("/setup" + ("?lang=ar" if lang == "ar" else ""))
    assert response.status_code == 200, response.status_code
    return CSRF_FIELD.search(response.text).group(1)


def submit(client: TestClient, password: str, confirm: str | None = None, *, token: str | None = None,
           lang: str = "en", headers: dict | None = None):
    token = setup_token(client, lang) if token is None else token
    return client.post(
        "/setup",
        data={"new_password": password, "confirm_password": password if confirm is None else confirm,
              "csrf_token": token, "lang": lang},
        headers=headers or {},
        follow_redirects=False,
    )


def login(client: TestClient, username: str, password: str):
    page = client.get("/login")
    assert page.status_code == 200, page.status_code
    token = CSRF_FIELD.search(page.text).group(1)
    return client.post("/login", data={"username": username, "password": password, "csrf_token": token},
                       follow_redirects=False)


def logout(client: TestClient) -> None:
    page = client.get("/pos")
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text).group(1)
    assert client.post("/logout", data={"csrf_token": token}, follow_redirects=False).status_code == 303


def provision(env: Env, *, username="owner", password=CHOSEN, role="admin", active=True) -> None:
    async def add(session):
        session.add(User(username=username, password_hash=auth.hash_password(password), role=role,
                         is_active=active, must_change_password=False))
        await session.commit()
    env.run(add)


def record_marker(env: Env, version: str) -> None:
    async def add(session):
        await session.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ))
        await session.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (:v)"), {"v": version})
        await session.commit()
    env.run(add)


def run_all_startups() -> None:
    from backend import desktop

    asyncio.run(seed.main())
    asyncio.run(desktop._seed_database())
    asyncio.run(database.upgrade_existing_database())
    with TestClient(app):
        pass


def build_v103(env: Env) -> None:
    """A v1.0.3 install: business tables, no auth tables, no accounts."""
    async def build():
        async with env.engine.begin() as connection:
            await connection.run_sync(database.Base.metadata.create_all)
            # v1.0.3 had neither accounts nor SIM cards (added in v1.0.4).
            for table in ("sim_cards", "user_sessions", "login_attempts", "users"):
                await connection.exec_driver_sql(f"DROP TABLE {table}")
    asyncio.run(build())


# ---------------------------------------------------------------------------
# 1. no default credential on any startup path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["source", "desktop", "web-upgrade"])
def test_fresh_startup_creates_no_account(env, entry) -> None:
    from backend import desktop

    if entry == "source":
        asyncio.run(seed.main())
    elif entry == "desktop":
        asyncio.run(desktop._seed_database())
    else:
        build_v103(env)
        with TestClient(app):  # web startup hook
            pass
    assert users(env) == []
    assert count(env, UserSession) == 0
    assert auth.INITIALIZED_MARKER not in markers(env)


def test_unauthenticated_visitors_are_sent_to_setup(client, blank) -> None:
    root = client.get("/", follow_redirects=True)
    assert root.status_code == 200
    assert root.url.path == "/setup"
    assert 'name="new_password"' in root.text and 'name="confirm_password"' in root.text

    for path in PROTECTED_PAGES:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"
    assert client.get("/pos?lang=ar", follow_redirects=False).headers["location"] == "/setup?lang=ar"

    for path in ("/login", "/login?lang=ar"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == ("/setup?lang=ar" if "ar" in path else "/setup")
    # A sign-in attempt is not processed at all before setup.
    response = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/setup"
    assert count(blank, LoginAttempt) == 0
    assert users(blank) == []


def test_apis_stay_closed_before_setup(client, blank) -> None:
    for path in PROTECTED_APIS:
        response = client.get(path)
        assert response.status_code == 401
        assert response.json() == {"detail": "Authentication required"}
    for method, path, body in (
        ("POST", "/api/users", {"username": "intruder"}),
        ("POST", "/api/customers", {"phone": "0933123456", "name": "Intruder"}),
        ("PUT", "/api/settings/timezone", {"timezone": "UTC"}),
    ):
        assert client.request(method, path, json=body).status_code == 401
    assert users(blank) == []
    assert client.get("/api/health").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 2. the setup page
# ---------------------------------------------------------------------------


def test_setup_page_is_bilingual_uncached_and_reveals_no_credential(client) -> None:
    english = client.get("/setup")
    assert english.status_code == 200
    assert english.headers["cache-control"] == "no-store"
    assert "<h1" in english.text and "Set up Uniter" in english.text
    assert "Choose the password for the administrator account." in english.text
    assert ">New password</label>" in english.text and ">Confirm password</label>" in english.text
    assert 'value="admin"' in english.text and "readonly" in english.text  # the fixed username
    assert 'id="setup-form"' in english.text and 'action="/setup"' in english.text
    for field in re.findall(r"<input[^>]*type=\"password\"[^>]*>", english.text):
        assert "value=" not in field
    assert "temporary password" not in english.text.lower()
    assert 'dir="ltr"' in english.text

    arabic = client.get("/setup?lang=ar")
    assert arabic.status_code == 200
    assert arabic.headers["cache-control"] == "no-store"
    assert 'dir="rtl"' in arabic.text and 'lang="ar"' in arabic.text
    assert "إعداد Uniter" in arabic.text
    assert "اختر كلمة مرور حساب المدير." in arabic.text
    assert ">كلمة المرور الجديدة</label>" in arabic.text and ">تأكيد كلمة المرور</label>" in arabic.text
    assert 'name="lang" value="ar"' in arabic.text
    assert 'href="/setup?lang=en"' in arabic.text and 'href="/setup?lang=ar"' in arabic.text


# ---------------------------------------------------------------------------
# 3. successful setup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_valid_setup_creates_exactly_one_admin_and_signs_in(client, blank, caplog, lang) -> None:
    caplog.set_level(logging.DEBUG)
    client.cookies.set(auth.SESSION_COOKIE, "stale-pre-setup-session")
    response = submit(client, CHOSEN, lang=lang)
    assert response.status_code == 303
    assert response.headers["location"] == ("/pos?lang=ar" if lang == "ar" else "/pos")
    assert response.headers["cache-control"] == "no-store"
    set_cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookies if c.startswith(f"{auth.SESSION_COOKIE}="))
    assert "HttpOnly" in session_cookie and "samesite=lax" in session_cookie.lower()
    assert "stale-pre-setup-session" not in session_cookie
    assert any(c.startswith(f"{auth.LOGIN_CSRF_COOKIE}=") and "Max-Age=0" in c for c in set_cookies)

    page = client.get("/pos")
    assert page.status_code == 200 and page.url.path == "/pos"
    assert client.get("/api/customers").status_code == 200
    assert client.get("/users").status_code == 200  # a real admin

    [admin] = users(blank)
    assert (admin.username, admin.role, admin.is_active, admin.must_change_password) == ("admin", "admin", True, False)
    assert admin.password_hash.startswith("scrypt$")
    assert auth.verify_password(CHOSEN, admin.password_hash)
    assert admin.password_changed_at is not None
    assert count(blank, UserSession) == 1
    assert auth.INITIALIZED_MARKER in markers(blank)

    # The chosen password is nowhere: not in the URL, the logs or the file.
    assert not _contains(response.headers["location"], CHOSEN)
    assert not _contains(caplog.text, CHOSEN), "the chosen password reached the logs"
    asyncio.run(blank.engine.dispose())
    assert not _contains(Path(blank.engine.url.database).read_bytes(), CHOSEN), "plaintext in the database"


@pytest.mark.parametrize(
    "password,confirm,message",
    [
        ("", "", "at least 8 characters"),
        ("short", "short", "at least 8 characters"),
        ("admin", "admin", "at least 8 characters"),
        ("ADMIN", "ADMIN", "at least 8 characters"),
        (CHOSEN, CHOSEN + "x", "do not match"),
        ("x" * 257, "x" * 257, "at most 256 characters"),
    ],
)
def test_unacceptable_passwords_create_nothing(client, blank, password, confirm, message) -> None:
    response = submit(client, password, confirm)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    error = SETUP_ERROR.search(response.text)
    assert error and message in error.group(1)
    # ("admin" itself is the read-only username shown on the page.)
    if password and password != auth.INITIAL_ADMIN_USERNAME:
        assert not _contains(response.text, password), "a posted password was reflected"
    assert users(blank) == [] and count(blank, UserSession) == 0
    assert auth.SESSION_COOKIE not in response.cookies
    # The form is still offered.
    assert 'id="setup-form"' in response.text


def test_password_equal_to_the_username_is_rejected(monkeypatch, client, blank) -> None:
    # "admin" is shorter than the minimum, so prove the username rule itself.
    monkeypatch.setattr(auth, "PASSWORD_MIN_LENGTH", 4)
    response = submit(client, " Admin ", " Admin ")
    assert response.status_code == 400
    assert "not the username" in SETUP_ERROR.search(response.text).group(1)
    assert users(blank) == []


def test_arabic_errors_are_translated(client) -> None:
    response = submit(client, CHOSEN, CHOSEN + "x", lang="ar")
    assert response.status_code == 400
    assert "غير متطابقتين" in SETUP_ERROR.search(response.text).group(1)
    assert 'dir="rtl"' in response.text


# ---------------------------------------------------------------------------
# 4. CSRF / origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["missing-token", "forged-token", "missing-cookie", "cross-origin"])
def test_setup_refuses_forged_or_cross_origin_posts(client, blank, case) -> None:
    token = setup_token(client)
    headers = {}
    if case == "missing-token":
        token = ""
    elif case == "forged-token":
        token = "f" * 43
    elif case == "missing-cookie":
        client.cookies.delete(auth.LOGIN_CSRF_COOKIE)
    else:
        headers = {"Origin": "https://evil.example"}
    response = submit(client, CHOSEN, token=token, headers=headers)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert "This setup form expired" in SETUP_ERROR.search(response.text).group(1)
    assert users(blank) == [] and count(blank, UserSession) == 0
    assert auth.SESSION_COOKIE not in response.cookies
    assert auth.INITIALIZED_MARKER not in markers(blank)


# ---------------------------------------------------------------------------
# 5. one-time only
# ---------------------------------------------------------------------------


def test_setup_is_permanently_unavailable_after_success(client, blank) -> None:
    assert submit(client, CHOSEN).status_code == 303
    admin_hash = users(blank)[0].password_hash

    # Signed in: the setup page sends you away; a repeat POST changes nothing.
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
    logout(client)

    for lang in ("en", "ar"):
        response = client.get("/setup" + ("?lang=ar" if lang == "ar" else ""), follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == ("/login?lang=ar" if lang == "ar" else "/login")
    # A well-formed repeat POST (valid double-submit pair from the login form).
    page = client.get("/login")
    token = CSRF_FIELD.search(page.text).group(1)
    again = submit(client, OTHER, token=token)
    assert again.status_code == 409
    assert again.headers["cache-control"] == "no-store"
    assert "already set up" in again.text and 'href="/login"' in again.text
    assert 'id="setup-form"' not in again.text
    assert auth.SESSION_COOKIE not in again.cookies
    assert users(blank)[0].password_hash == admin_hash
    assert len(users(blank)) == 1 and count(blank, UserSession) == 0

    # Sign-in requires the password chosen at setup, and only that.
    assert login(client, "admin", OTHER).status_code == 401
    ok = login(client, "admin", CHOSEN)
    assert ok.status_code == 303 and ok.headers["location"] == "/pos"


def test_a_stale_setup_form_loses_safely(blank) -> None:
    with TestClient(app) as slow, TestClient(app) as fast:
        stale_token = setup_token(slow)
        assert submit(fast, CHOSEN).status_code == 303
        late = submit(slow, OTHER, token=stale_token)
        assert late.status_code == 409
        assert "already set up" in late.text
        assert auth.SESSION_COOKIE not in late.cookies
    [admin] = users(blank)
    assert auth.verify_password(CHOSEN, admin.password_hash)
    assert not auth.verify_password(OTHER, admin.password_hash)
    assert count(blank, UserSession) == 1


@pytest.fixture
def racing(tmp_path, monkeypatch) -> Iterator[Env]:
    """Each request gets its own connection, so two clients really race."""
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(tmp_path / "race.db")), poolclass=NullPool
    )
    event.listen(engine.sync_engine, "connect", database.enable_sqlite_foreign_keys)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "DATABASE_URL", engine.url)
    monkeypatch.setattr(seed, "SessionLocal", sessions)
    asyncio.run(seed.main())

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        yield Env(engine, sessions)
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


def test_concurrent_setup_submissions_have_exactly_one_winner(racing) -> None:
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def attempt(name: str, password: str) -> None:
        with TestClient(app) as browser:
            token = setup_token(browser)
            barrier.wait()
            response = submit(browser, password, token=token)
            results[name] = (response.status_code, auth.SESSION_COOKIE in response.cookies)

    threads = [threading.Thread(target=attempt, args=("a", CHOSEN)), threading.Thread(target=attempt, args=("b", OTHER))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(results.values()) == [(303, True), (409, False)]
    winner = CHOSEN if results["a"][0] == 303 else OTHER
    loser = OTHER if winner == CHOSEN else CHOSEN
    [admin] = users(racing)
    assert auth.verify_password(winner, admin.password_hash)
    assert not auth.verify_password(loser, admin.password_hash)
    assert count(racing, UserSession) == 1


def test_initialize_admin_is_atomic_under_a_race(racing) -> None:
    hashes = [auth.hash_password(CHOSEN), auth.hash_password(OTHER)]
    other_engine = create_async_engine(racing.engine.url, poolclass=NullPool)

    async def race():
        return await asyncio.gather(
            auth.initialize_admin(racing.engine, hashes[0]),
            auth.initialize_admin(other_engine, hashes[1]),
        )

    try:
        outcomes = asyncio.run(race())
    finally:
        asyncio.run(other_engine.dispose())
    assert sum(token is not None for token in outcomes) == 1
    [admin] = users(racing)
    assert admin.password_hash == hashes[outcomes.index(next(t for t in outcomes if t is not None))]
    assert count(racing, UserSession) == 1
    assert auth.INITIALIZED_MARKER in markers(racing)


# ---------------------------------------------------------------------------
# 6. existing / legacy databases
# ---------------------------------------------------------------------------


def test_existing_accounts_never_see_setup_and_keep_credentials(client, blank) -> None:
    provision(blank, username="owner", password=CHOSEN)
    before = [(u.username, u.password_hash, u.role, u.is_active) for u in users(blank)]
    run_all_startups()
    assert [(u.username, u.password_hash, u.role, u.is_active) for u in users(blank)] == before
    assert auth.INITIALIZED_MARKER in markers(blank)

    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/pos", follow_redirects=False).headers["location"].startswith("/login")
    assert 'name="password"' in client.get("/login").text
    ok = login(client, "owner", CHOSEN)
    assert ok.status_code == 303 and ok.headers["location"] == "/pos"


def test_users_without_a_marker_count_as_initialised_permanently(env) -> None:
    """Legacy transition: accounts present but no marker -> the marker is
    recorded at startup, accounts are untouched, setup never opens."""
    build_v103(env)

    async def legacy_users():
        async with env.engine.begin() as connection:
            await connection.run_sync(lambda sync: database.Base.metadata.create_all(
                sync, tables=[User.__table__, UserSession.__table__, LoginAttempt.__table__]
            ))
    asyncio.run(legacy_users())
    provision(env, username="owner", password=CHOSEN)
    provision(env, username="sara", password=OTHER, role="seller")
    before = [(u.username, u.password_hash) for u in users(env)]
    assert auth.INITIALIZED_MARKER not in markers(env)

    asyncio.run(database.upgrade_existing_database())
    assert auth.INITIALIZED_MARKER in markers(env)
    assert [(u.username, u.password_hash) for u in users(env)] == before

    # Even if every account later vanished, setup would not reopen.
    async def wipe(session):
        await session.execute(UserSession.__table__.delete())
        await session.execute(User.__table__.delete())
        await session.commit()
    env.run(wipe)
    run_all_startups()
    assert users(env) == []
    with TestClient(app) as browser:
        assert browser.get("/setup").status_code == 503


def test_legacy_bootstrap_marker_keeps_its_account(env) -> None:
    """A database bootstrapped by an earlier v1.0.4 build stays as it is."""
    asyncio.run(seed.main())
    record_marker(env, auth.LEGACY_BOOTSTRAP_MARKER)
    provision(env, username="admin", password=CHOSEN)
    before = [(u.username, u.password_hash, u.must_change_password) for u in users(env)]
    run_all_startups()
    assert [(u.username, u.password_hash, u.must_change_password) for u in users(env)] == before
    with TestClient(app) as browser:
        assert browser.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_unchanged_legacy_bootstrap_password_no_longer_signs_in(env) -> None:
    """An earlier review build's bootstrap account that never left its retired
    shared password is kept (never reset) but that password is refused: every
    password the app accepts is at least PASSWORD_MIN_LENGTH long, so only the
    retired default can be shorter. Recovery is the documented local path."""
    retired = auth.INITIAL_ADMIN_USERNAME  # the retired default equalled the username
    asyncio.run(seed.main())
    record_marker(env, auth.LEGACY_BOOTSTRAP_MARKER)
    provision(env, username="admin", password=retired)
    async def force_change(session):
        await session.execute(User.__table__.update().values(must_change_password=True))
        await session.commit()
    env.run(force_change)
    before = [(u.username, u.password_hash, u.must_change_password) for u in users(env)]
    run_all_startups()
    with TestClient(app) as browser:
        refused = login(browser, "admin", retired)
        assert refused.status_code == 401
        assert auth.SESSION_COOKIE not in refused.cookies
        assert browser.get("/setup", follow_redirects=False).headers["location"] == "/login"
    assert count(env, UserSession) == 0
    assert [(u.username, u.password_hash, u.must_change_password) for u in users(env)] == before


# ---------------------------------------------------------------------------
# 7. damaged state: initialised but no active admin -> fail closed
# ---------------------------------------------------------------------------


def _assert_locked(env: Env, browser: TestClient) -> None:
    before = [(u.username, u.password_hash, u.is_active) for u in users(env)]
    for lang, heading in (("en", "Sign-in is unavailable"), ("ar", "تسجيل الدخول غير متاح")):
        page = browser.get("/setup" + ("?lang=ar" if lang == "ar" else ""))
        assert page.status_code == 503
        assert page.headers["cache-control"] == "no-store"
        assert heading in page.text
        assert 'id="setup-locked"' in page.text and 'id="setup-form"' not in page.text
        assert "README" in page.text
    assert browser.get("/", follow_redirects=False).status_code == 302
    for path in ("/pos", "/login", "/users"):
        response = browser.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/setup"
    assert browser.get("/api/customers").status_code == 503
    # A well-formed setup POST cannot take the database over.
    browser.cookies.set(auth.LOGIN_CSRF_COOKIE, "t" * 43)
    takeover = browser.post("/setup", data={"new_password": CHOSEN, "confirm_password": CHOSEN,
                                            "csrf_token": "t" * 43}, follow_redirects=False)
    assert takeover.status_code == 503
    assert auth.SESSION_COOKIE not in takeover.cookies
    login_attempt = browser.post("/login", data={"username": "admin", "password": CHOSEN,
                                                 "csrf_token": "t" * 43}, follow_redirects=False)
    assert login_attempt.status_code in (303, 503)
    assert auth.SESSION_COOKIE not in login_attempt.cookies
    assert [(u.username, u.password_hash, u.is_active) for u in users(env)] == before


def test_marker_without_any_account_fails_closed(blank) -> None:
    record_marker(blank, auth.INITIALIZED_MARKER)
    with TestClient(app) as browser:
        _assert_locked(blank, browser)
    assert users(blank) == []


def test_accounts_without_an_active_admin_fail_closed_even_for_signed_in_sellers(blank) -> None:
    from backend.auth_test_support import sign_in

    provision(blank, username="owner", password=CHOSEN, active=False)
    with TestClient(app) as seller:
        sign_in(seller, blank.sessions, role="seller", username="sara")
        assert seller.get("/api/transactions").status_code == 503
        assert seller.get("/pos", follow_redirects=False).headers["location"] == "/setup"
        _assert_locked(blank, seller)


# ---------------------------------------------------------------------------
# 8. no shared default anywhere; the rest of the product still works
# ---------------------------------------------------------------------------


def test_no_shared_default_password_in_source_or_docs() -> None:
    source = (ROOT / "backend" / "auth.py").read_text()
    assert "BOOTSTRAP_PASSWORD" not in source
    assert not re.search(r"hash_password\(\s*['\"]", source)
    assert not hasattr(auth, "BOOTSTRAP_PASSWORD")
    for path in ("README.md", "backend/desktop.py", "backend/database.py"):
        text_ = (ROOT / path).read_text()
        assert "admin/admin" not in text_
        assert not re.search(r"(?i)temporary password\s+`?admin`?", text_)


def test_after_setup_accounts_attribution_and_forced_change_still_work(client, blank) -> None:
    assert submit(client, CHOSEN).status_code == 303
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/pos").text).group(1)
    client.headers[auth.CSRF_HEADER] = token
    created = client.post("/api/users", json={"username": "sara"})
    assert created.status_code == 201
    temporary = created.json()["temporary_password"]
    for code in ("mtn",):
        assert client.post("/api/operators/topup", json={"operator_code": code, "amount_cents": 100000}).status_code == 200

    with TestClient(app) as seller:
        first = login(seller, "sara", temporary)
        assert first.status_code == 303 and first.headers["location"] == "/change-password"
        assert seller.get("/pos", follow_redirects=False).headers["location"].startswith("/change-password")
        page = seller.get("/change-password")
        change_token = CSRF_FIELD.search(page.text).group(1)
        changed = seller.post("/change-password", data={
            "current_password": temporary, "new_password": OTHER, "confirm_password": OTHER,
            "csrf_token": change_token,
        }, follow_redirects=False)
        assert changed.status_code == 303
        seller_token = re.search(r'<meta name="csrf-token" content="([^"]+)"', seller.get("/pos").text).group(1)
        sale = seller.post("/api/transactions", headers={auth.CSRF_HEADER: seller_token}, json={
            "customer_phone": "0933123456", "operator_code": "mtn", "service_code": "prepaid",
            "amount_cents": 1000, "commit_key": "setup-slice-commit-0001",
        })
        assert sale.status_code == 200, sale.text

    async def performer(session):
        return await session.scalar(
            select(User.username).join(Transaction, Transaction.performed_by_user_id == User.id)
        )
    assert blank.run(performer) == "sara"
