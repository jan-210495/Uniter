import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, delete, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, CurrencyDenomination, Setting, enable_sqlite_foreign_keys, get_db
from backend.main import app
from backend.seed import generate_ussd, seed_data


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


def remove_setting(key: str) -> None:
    async def remove() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            await session.execute(delete(Setting).where(Setting.key == key))
            await session.commit()

    asyncio.run(remove())


def test_mtn_domain_get_returns_seeded_default(client: TestClient) -> None:
    assert client.get("/api/settings/mtn-domain").json() == {"mtn_domain": "65477"}


def test_mtn_domain_get_falls_back_when_not_set(client: TestClient) -> None:
    remove_setting("MTN_DOMAIN")
    assert client.get("/api/settings/mtn-domain").json() == {"mtn_domain": "65477"}


def test_mtn_domain_put_updates_and_persists(client: TestClient) -> None:
    update = client.put("/api/settings/mtn-domain", json={"domain": "55501"})
    assert update.status_code == 200
    assert update.json() == {"mtn_domain": "55501"}
    assert client.get("/api/settings/mtn-domain").json() == {"mtn_domain": "55501"}

    # generate_ussd consumes the stored domain for new transactions.
    async def check_ussd() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            assert (
                await generate_ussd(session, "mtn", "prepaid", "0968663410", 5000)
                == "*150*55501*0968663410*50#"
            )

    asyncio.run(check_ussd())
    # The page prefills the input with the stored value.
    assert 'id="mtn-domain-input"' in client.get("/settings").text
    assert 'value="55501"' in client.get("/settings").text


@pytest.mark.parametrize("domain", ["12a45", "", "  ", "12 45", "+963", "abc", "#*150", "6547a"])
def test_mtn_domain_put_rejects_invalid_domains(client: TestClient, domain: str) -> None:
    assert client.put("/api/settings/mtn-domain", json={"domain": domain}).status_code == 422
    assert client.get("/api/settings/mtn-domain").json() == {"mtn_domain": "65477"}


def test_mtn_domain_put_length_boundaries(client: TestClient) -> None:
    twenty = "1" * 20
    assert client.put("/api/settings/mtn-domain", json={"domain": twenty}).status_code == 200
    assert client.get("/api/settings/mtn-domain").json() == {"mtn_domain": twenty}
    assert client.put("/api/settings/mtn-domain", json={"domain": "1" * 21}).status_code == 422
    assert client.get("/api/settings/mtn-domain").json() == {"mtn_domain": twenty}


def test_syriatel_domain_get_returns_seeded_default(client: TestClient) -> None:
    assert client.get("/api/settings/syriatel-domain").json() == {"syriatel_domain": "67858"}


def test_syriatel_domain_get_falls_back_when_not_set(client: TestClient) -> None:
    remove_setting("SYRIATEL_DOMAIN")
    assert client.get("/api/settings/syriatel-domain").json() == {"syriatel_domain": "67858"}


def test_syriatel_domain_put_updates_and_persists(client: TestClient) -> None:
    update = client.put("/api/settings/syriatel-domain", json={"domain": "24680"})
    assert update.status_code == 200
    assert update.json() == {"syriatel_domain": "24680"}
    assert client.get("/api/settings/syriatel-domain").json() == {"syriatel_domain": "24680"}

    async def check_ussd() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            assert (
                await generate_ussd(session, "syriatel", "postpaid", "0968663410", 5000)
                == "*150*1*24680*5*50*0968663410*0968663410#"
            )

    asyncio.run(check_ussd())


@pytest.mark.parametrize("domain", ["12a45", "", "  ", "12 45", "6785a"])
def test_syriatel_domain_put_rejects_invalid_domains(client: TestClient, domain: str) -> None:
    assert client.put("/api/settings/syriatel-domain", json={"domain": domain}).status_code == 422
    assert client.get("/api/settings/syriatel-domain").json() == {"syriatel_domain": "67858"}


def test_settings_page_shows_domain_inputs(client: TestClient) -> None:
    english = client.get("/settings")
    arabic = client.get("/settings", params={"lang": "ar"})
    assert english.status_code == arabic.status_code == 200
    # Both inputs prefilled with the current (default) values.
    assert 'id="mtn-domain-input"' in english.text
    assert 'value="65477"' in english.text
    assert 'id="syriatel-domain-input"' in english.text
    assert 'value="67858"' in english.text
    assert "MTN domain" in english.text
    assert "Syriatel domain" in english.text
    assert 'value="65477"' in arabic.text
    assert 'value="67858"' in arabic.text
    assert "نطاق MTN" in arabic.text
    assert "نطاق Syriatel" in arabic.text
    # The forms submit to the new endpoints with live feedback regions.
    assert "wireDomainForm(" in english.text
    assert "'/api/settings/mtn-domain'" in english.text
    assert "'/api/settings/syriatel-domain'" in english.text
    assert 'id="mtn-domain-feedback"' in english.text
    assert 'id="syriatel-domain-feedback"' in english.text


def test_timezone_get_returns_default(client: TestClient) -> None:
    assert client.get("/api/settings/timezone").json() == {"timezone": "Asia/Damascus"}


def test_timezone_put_updates_and_persists(client: TestClient) -> None:
    update = client.put("/api/settings/timezone", json={"timezone": "Europe/Zurich"})
    assert update.status_code == 200
    assert update.json() == {"timezone": "Europe/Zurich"}
    assert client.get("/api/settings/timezone").json() == {"timezone": "Europe/Zurich"}
    # The page select reflects the stored value.
    assert '<option value="Europe/Zurich" selected>' in client.get("/settings").text


@pytest.mark.parametrize("timezone", ["", "x" * 65])
def test_timezone_put_rejects_invalid_values(client: TestClient, timezone: str) -> None:
    assert client.put("/api/settings/timezone", json={"timezone": timezone}).status_code == 422
    assert client.get("/api/settings/timezone").json() == {"timezone": "Asia/Damascus"}


def test_timezone_put_requires_field(client: TestClient) -> None:
    assert client.put("/api/settings/timezone", json={}).status_code == 422
    assert client.get("/api/settings/timezone").json() == {"timezone": "Asia/Damascus"}


def test_settings_page_shows_timezone_select(client: TestClient) -> None:
    english = client.get("/settings")
    arabic = client.get("/settings", params={"lang": "ar"})
    assert english.status_code == arabic.status_code == 200
    for rendered in (english.text, arabic.text):
        assert 'id="timezone-select"' in rendered
        assert 'id="timezone-form"' in rendered
        assert 'id="timezone-feedback"' in rendered
        # Common options with the default selected.
        for zone in ("UTC", "Asia/Damascus", "Europe/Damascus", "America/New_York"):
            assert f'<option value="{zone}"' in rendered
        assert '<option value="Asia/Damascus" selected>' in rendered
        assert "fetch('/api/settings/timezone'" in rendered
    assert "Timezone" in english.text
    assert "المنطقة الزمنية" in arabic.text


def test_base_template_renders_burger_menu_and_drawer(client: TestClient) -> None:
    for path in ("/pos", "/settings", "/customers"):
        english = client.get(path)
        arabic = client.get(path, params={"lang": "ar"})
        assert english.status_code == arabic.status_code == 200
        for rendered in (english.text, arabic.text):
            # Burger button: small screens only, wired to the drawer.
            assert 'id="nav-toggle"' in rendered
            assert 'class="shell-mobile-bar"' in rendered
            assert 'aria-expanded="false"' in rendered
            assert 'aria-controls="app-drawer"' in rendered
            # Off-canvas drawer with navigation semantics.
            assert 'id="app-drawer"' in rendered
            assert 'role="navigation"' in rendered
            assert 'id="app-drawer" class="drawer-closed" role="navigation"' in rendered
            assert 'id="app-drawer-backdrop"' in rendered
            assert 'id="app-drawer-close"' in rendered
            # Every page link plus the language switcher inside the drawer.
            for href in ("/pos", "/settlement", "/transactions", "/customers", "/settings", "/topup"):
                assert f'href="{href}"' in rendered
            assert "?lang=en" in rendered and "?lang=ar" in rendered
            # Escape closes; the drawer slides from the start edge.
            script = client.get('/static/navigation.js').text
            css = client.get('/static/navigation.css').text
            assert "Escape" in script
            assert "drawer-closed" in rendered
            assert "translateX(-100%)" in css
            assert "translateX(100%)" in css
            assert "(min-width: 1024px)" in css
            # Header timezone slot filled by the shared script.
            assert 'id="header-timezone"' in rendered
            assert "fetch('/api/settings/timezone'" in rendered
    # The drawer labels follow the page language.
    assert "Navigation menu" in client.get("/pos").text
    assert "قائمة التنقل" in client.get("/pos", params={"lang": "ar"}).text
