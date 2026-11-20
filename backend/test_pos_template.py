import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, enable_sqlite_foreign_keys, get_db
from backend.auth_test_support import sign_in
from backend.main import app
from backend.seed import seed_data


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "pos.db")))
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
            sign_in(test_client, sessions)
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


def test_pos_template_renders_with_base() -> None:
    environment = Environment(loader=FileSystemLoader("backend/templates"), autoescape=True)
    template = environment.get_template("pos.html")
    english = template.render(lang="en")
    arabic = template.render(lang="ar")
    assert '<html lang="en" dir="ltr">' in english
    assert '<html lang="ar" dir="rtl">' in arabic
    assert "POS Quick Charge" in english
    assert "الشحن السريع" in arabic
    assert 'src="https://cdn.tailwindcss.com"' in english
    assert 'class="grid gap-6 md:grid-cols-2"' in english
    for amount in (10, 20, 50, 100, 200):
        assert f'data-amount="{amount}"' in english
    assert "navigator.clipboard.writeText" in english
    assert "fetch('/api/settings/commission'" in english


def test_pos_template_has_manual_phone_and_optional_snapshot_name() -> None:
    environment = Environment(loader=FileSystemLoader("backend/templates"), autoescape=True)
    for lang in ("en", "ar"):
        rendered = environment.get_template("pos.html").render(lang=lang)
        assert 'id="phone"' in rendered
        assert 'type="tel" dir="ltr"' in rendered
        assert 'autofocus required pattern=' in rendered
        assert 'id="customer-name"' in rendered
        assert 'maxlength="255"' in rendered
        for removed in ("customer-suggestions", "customer-info", "customer-search-status", "/api/customers"):
            assert removed not in rendered
        assert "normalizePhone(phone.value)" in rendered
        assert "customer_name: saleName" in rendered


def test_pos_route_languages(client: TestClient) -> None:
    english = client.get("/pos")
    arabic = client.get("/pos", params={"lang": "ar"})
    fallback = client.get("/pos", params={"lang": "invalid"})
    assert english.status_code == arabic.status_code == fallback.status_code == 200
    assert "POS Quick Charge" in english.text
    assert '<html lang="ar" dir="rtl">' in arabic.text
    assert "الشحن السريع" in arabic.text
    assert '<html lang="en" dir="ltr">' in fallback.text
    assert 'id="customer-suggestions"' not in english.text
    assert 'id="customer-name"' in arabic.text
