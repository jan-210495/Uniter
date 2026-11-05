import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, Operator, enable_sqlite_foreign_keys, get_db
from backend.main import app
from backend.seed import seed_data


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "operator.db")))
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


def topup(client: TestClient, operator_code: str, amount_cents: int) -> None:
    assert client.post(
        "/api/operators/topup", json={"operator_code": operator_code, "amount_cents": amount_cents}
    ).status_code == 200


def post_transaction(client: TestClient, phone: str, operator: str, service: str, amount: int) -> dict:
    response = client.post(
        "/api/transactions",
        json={
            "customer_phone": phone,
            "operator_code": operator,
            "service_code": service,
            "amount_cents": amount,
        },
    )
    assert response.status_code == 200
    return response.json()


def test_operator_page_renders_mtn(client: TestClient) -> None:
    topup(client, "mtn", 150000)
    html = client.get("/operators/mtn")
    assert html.status_code == 200
    assert "MTN" in html.text
    assert "1,500" in html.text
    # Code badge, top-up link and the eight-column transactions table.
    assert 'dir="ltr">mtn<' in html.text
    assert 'href="/topup"' in html.text
    assert html.text.count('scope="col"') == 8
    for header in ("Reference", "Date/time", "Customer", "Operator", "Service",
                   "Amount", "Status", "USSD"):
        assert header in html.text


def test_operator_page_renders_syriatel(client: TestClient) -> None:
    html = client.get("/operators/syriatel")
    assert html.status_code == 200
    assert "Syriatel" in html.text
    assert 'dir="ltr">syriatel<' in html.text
    # No balance row exists yet: zero is shown.
    assert "0" in html.text


def test_operator_page_unknown_operator_is_404(client: TestClient) -> None:
    assert client.get("/operators/unknown").status_code == 404
    # Operator codes are exact; the uppercase name is not a code.
    assert client.get("/operators/MTN").status_code == 404


def test_operator_page_inactive_operator_is_404(client: TestClient) -> None:
    async def deactivate() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            operator = await session.scalar(select(Operator).where(Operator.code == "mtn"))
            operator.is_active = False
            await session.commit()

    asyncio.run(deactivate())
    assert client.get("/operators/mtn").status_code == 404


def test_operator_page_languages(client: TestClient) -> None:
    english = client.get("/operators/mtn")
    arabic = client.get("/operators/mtn", params={"lang": "ar"})
    assert english.status_code == arabic.status_code == 200
    assert '<html lang="en" dir="ltr">' in english.text
    assert "Remaining balance" in english.text
    assert "Operator transactions" in english.text
    assert '<html lang="ar" dir="rtl">' in arabic.text
    assert "الرصيد المتبقي" in arabic.text
    assert "معاملات المشغّل" in arabic.text
    assert "لا توجد معاملات لهذا المشغّل بعد." in arabic.text


def test_operator_page_shows_only_that_operators_transactions(client: TestClient) -> None:
    topup(client, "mtn", 1000000)
    topup(client, "syriatel", 1000000)
    client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"})
    client.post("/api/customers", json={"phone": "0944123456", "name": "Rami"})
    mtn_transactions = [
        post_transaction(client, "0933123456", "mtn", "prepaid", 1000),
        post_transaction(client, "0933123456", "mtn", "prepaid", 2000),
        post_transaction(client, "0933123456", "mtn", "postpaid", 3000),
    ]
    post_transaction(client, "0944123456", "syriatel", "prepaid", 5000)
    post_transaction(client, "0944123456", "syriatel", "postpaid", 6000)

    html = client.get("/operators/mtn").text
    for transaction in mtn_transactions:
        assert transaction["reference"] in html
    # Syriatel-only transactions (and their USSD strings) are absent.
    assert "0944123456" not in html
    assert "*150*1*" not in html
    # Newest first: the last created MTN transaction appears before the others.
    positions = [html.index(transaction["reference"]) for transaction in mtn_transactions]
    assert positions == sorted(positions, reverse=True)
    # Balance card reflects deductions from this operator only.
    assert "9,940" in html  # 1,000,000 - 6,000 face value = 9,940 SYP


def test_operator_page_empty_state(client: TestClient) -> None:
    html = client.get("/operators/mtn")
    assert "No transactions for this operator yet." in html.text


def test_operator_page_links_from_settings_and_transactions(client: TestClient) -> None:
    assert 'href="/operators/mtn"' in client.get("/settings").text
    assert 'href="/operators/syriatel"' in client.get("/settings").text
    # The transactions page script links operator cells to the subpage.
    assert "'/operators/' + encodeURIComponent(transaction.operator_code)" in client.get(
        "/transactions"
    ).text


def test_base_template_renders_global_search(client: TestClient) -> None:
    for path in ("/pos", "/settings", "/customers"):
        english = client.get(path)
        arabic = client.get(path, params={"lang": "ar"})
        assert english.status_code == arabic.status_code == 200
        for rendered in (english.text, arabic.text):
            # Desktop input in the header, mobile input in the drawer.
            assert 'id="global-search"' in rendered
            assert 'id="global-search-anchor"' in rendered
            assert 'id="drawer-global-search"' in rendered
            assert 'id="drawer-search-anchor"' in rendered
            # Combobox semantics over one shared listbox.
            assert 'role="combobox"' in rendered
            assert 'aria-controls="global-search-results"' in rendered
            assert 'aria-autocomplete="list"' in rendered
            assert 'id="global-search-results"' in rendered
            assert 'role="listbox"' in rendered
            # Debounced, abortable search over the new endpoint.
            assert "const SEARCH_DEBOUNCE_MS = 220;" in rendered
            assert "new AbortController()" in rendered
            assert "'/api/search?q=' + encodeURIComponent(query)" in rendered
    assert "Search customers, transactions, operators" in client.get("/pos").text
    assert "البحث عن العملاء والمعاملات والمشغّلين" in client.get(
        "/pos", params={"lang": "ar"}
    ).text
