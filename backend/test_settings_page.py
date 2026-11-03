import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, CurrencyDenomination, enable_sqlite_foreign_keys, get_db
from backend.main import app
from backend.seed import seed_data


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "settings.db")))
    event.listen(engine.sync_engine, "connect", enable_sqlite_foreign_keys)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            await seed_data(session)

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


def add_denomination(scale_name: str, multiplier: int, active: bool = False) -> None:
    """Insert a currency denomination directly (no creation endpoint exists)."""

    async def insert() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            session.add(
                CurrencyDenomination(scale_name=scale_name, multiplier=multiplier, is_active=active)
            )
            await session.commit()

    asyncio.run(insert())


def test_settings_page_renders_in_both_languages(client: TestClient) -> None:
    english = client.get("/settings")
    arabic = client.get("/settings", params={"lang": "ar"})
    fallback = client.get("/settings", params={"lang": "invalid"})
    assert english.status_code == arabic.status_code == fallback.status_code == 200
    assert "Settings" in english.text
    assert "Commission rate" in english.text
    assert "Currency denomination" in english.text
    assert "Code structure" in english.text
    assert '<html lang="ar" dir="rtl">' in arabic.text
    assert "الإعدادات" in arabic.text
    assert "نسبة العمولة" in arabic.text
    assert "فئة العملة" in arabic.text
    assert "هيكل الرموز" in arabic.text
    assert '<html lang="en" dir="ltr">' in fallback.text
    # Shared navigation exposes the new page and highlights it.
    assert 'href="/settings"' in english.text
    assert 'aria-current="page"' in english.text
    # The page updates settings through the existing endpoints.
    assert "fetch('/api/settings/commission'" in english.text
    assert "fetch('/api/currency/denomination'" in english.text


def test_settings_page_renders_code_structure(client: TestClient) -> None:
    html = client.get("/settings").text
    for operator in ("MTN", "Syriatel"):
        assert operator in html
    # USSD prefixes rendered in their own cells.
    assert ">100</td>" in html
    assert ">555</td>" in html
    for service_name in ("Pre-Paid Charging", "Post-Paid Bill Payment"):
        assert service_name in html
    for service_code in (">prepaid</td>", ">postpaid</td>"):
        assert service_code in html
    assert "Active" in html
    # Table header labels.
    for header in ("Operator", "Service", "Service code", "Status"):
        assert header in html


def test_settings_page_lists_denominations_with_active_selected(client: TestClient) -> None:
    add_denomination("pre-2026", 500)
    html = client.get("/settings").text
    assert '<option value="post-2026" selected>' in html
    assert '<option value="pre-2026">' in html
    # Active denomination summary shows the seeded scale.
    assert 'id="denom-active-name"' in html


def test_commission_get_returns_current_rate(client: TestClient) -> None:
    response = client.get("/api/settings/commission")
    assert response.status_code == 200
    assert response.json() == {"commission_rate": "0.05"}


def test_commission_put_updates_rate(client: TestClient) -> None:
    update = client.put("/api/settings/commission", json={"rate": "0.12"})
    assert update.status_code == 200
    assert update.json() == {"commission_rate": "0.12"}
    # The update persists for later requests.
    assert client.get("/api/settings/commission").json() == {"commission_rate": "0.12"}


@pytest.mark.parametrize("rate", ["-0.01", "1.01", "1.5", "abc", "NaN", "Infinity"])
def test_commission_put_rejects_invalid_rate(client: TestClient, rate: str) -> None:
    assert client.put("/api/settings/commission", json={"rate": rate}).status_code == 422
    assert client.get("/api/settings/commission").json() == {"commission_rate": "0.05"}


def test_denomination_get_returns_active_denomination(client: TestClient) -> None:
    response = client.get("/api/currency/denomination")
    assert response.status_code == 200
    data = response.json()
    assert data["scale_name"] == "post-2026"
    assert data["multiplier"] == 1
    assert data["is_active"] is True
    assert isinstance(data["id"], int)


def test_denomination_put_switches_active(client: TestClient) -> None:
    add_denomination("pre-2026", 500)

    update = client.put("/api/currency/denomination", json={"scale_name": "pre-2026"})
    assert update.status_code == 200
    assert update.json()["scale_name"] == "pre-2026"
    assert update.json()["is_active"] is True

    # The switch persists: the active denomination endpoint reports the new one.
    assert client.get("/api/currency/denomination").json()["scale_name"] == "pre-2026"
    # And a fresh page render marks the new scale as selected.
    assert '<option value="pre-2026" selected>' in client.get("/settings").text

    # Unknown denominations fail cleanly without changing state.
    assert client.put("/api/currency/denomination", json={"scale_name": "nope"}).status_code == 404
    assert client.get("/api/currency/denomination").json()["scale_name"] == "pre-2026"
