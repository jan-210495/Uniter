"""Unauthenticated requests must never reach the normal pages or APIs.

These checks use plain HTTP only so they describe the externally visible
contract: protected HTML pages redirect to the sign-in page, protected APIs
answer 401, and only the minimal public surface stays reachable.
"""
import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.auth import hash_password
from backend.database import Base, User, enable_sqlite_foreign_keys, get_db
from backend.main import app
from backend.seed import seed_data

PROTECTED_PAGES = (
    "/pos", "/topup", "/transactions", "/stats",
    "/settings", "/operators/mtn", "/pos?lang=ar",
)

PROTECTED_API_READS = (
    "/api/transactions",
    "/api/sims",
    "/api/search?q=09",
    "/api/stats/summary",
    "/api/settings/commission",
    "/api/settings/timezone",
    "/api/operators/balance",
    "/api/currency/denomination",
    "/api/register/summary",
    "/api/commission/calculate?operator_code=mtn&service_code=prepaid&amount_cents=100",
)

PROTECTED_API_WRITES = (
    ("put", "/api/settings/commission", {"rate": "0.5"}),
    ("put", "/api/settings/timezone", {"timezone": "UTC"}),
    ("post", "/api/operators/topup", {"operator_code": "mtn", "amount_cents": 100}),
    ("post", "/api/transactions", {
        "customer_phone": "0933123456", "operator_code": "mtn",
        "service_code": "prepaid", "amount_cents": 100,
    }),
    ("patch", "/api/transactions/1/status", {"status": "failed"}),
    ("delete", "/api/transactions/1", None),
    ("put", "/api/currency/denomination", {"scale_name": "post-2026"}),
)


@pytest.fixture
def anonymous(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "anon.db")))
    event.listen(engine.sync_engine, "connect", enable_sqlite_foreign_keys)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            await seed_data(session)
            # An installed system: its admin exists (password random and
            # unused), so this is the anonymous-visitor contract, not first-run
            # setup (covered by test_initial_setup).
            session.add(User(username="admin", password_hash=hash_password(secrets.token_urlsafe(16)),
                             role="admin", is_active=True, must_change_password=False))
            await session.commit()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    asyncio.run(setup())
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


@pytest.mark.parametrize("path", PROTECTED_PAGES)
def test_protected_pages_redirect_to_sign_in(anonymous, path):
    response = anonymous.get(path, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/login")
    assert "Uniter" not in response.text  # no page content leaks with the redirect


@pytest.mark.parametrize("path", PROTECTED_API_READS)
def test_protected_api_reads_are_rejected(anonymous, path):
    response = anonymous.get(path, follow_redirects=False)
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


@pytest.mark.parametrize("method,path,body", PROTECTED_API_WRITES)
def test_protected_api_writes_are_rejected(anonymous, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    response = anonymous.request(method.upper(), path, follow_redirects=False, **kwargs)
    assert response.status_code == 401
    # Nothing was written behind the rejection.
    assert anonymous.get("/api/health").status_code == 200


def test_forged_session_cookie_is_rejected(anonymous):
    anonymous.cookies.set("uniter_session", "not-a-real-session")
    assert anonymous.get("/pos", follow_redirects=False).status_code == 303
    assert anonymous.get("/api/transactions").status_code == 401


def test_minimal_public_surface_stays_reachable(anonymous):
    assert anonymous.get("/api/health").json() == {"status": "ok"}
    assert anonymous.get("/manifest.webmanifest").status_code == 200
    assert anonymous.get("/static/navigation.css").status_code == 200
    assert anonymous.get("/static/csrf.js").status_code == 200
    login = anonymous.get("/login")
    assert login.status_code == 200
    assert 'name="password"' in login.text
    assert anonymous.get("/login?lang=ar").text.count('dir="rtl"') >= 1
    root = anonymous.get("/", follow_redirects=False)
    assert root.status_code == 302 and root.headers["location"] == "/pos"


def test_interactive_api_docs_are_not_exposed(anonymous):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert anonymous.get(path).status_code == 404
