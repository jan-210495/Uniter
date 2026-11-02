"""Tests for the /customers page and its CRUD endpoints."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, Customer, enable_sqlite_foreign_keys, get_db
from backend.main import app
from backend.seed import seed_data

AHMAD_PHONE = "0933123456"
RAMI_PHONE = "0944123456"


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(tmp_path / "customers.db"))
    )
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


def create_customer(client: TestClient, phone: str, name: str) -> dict:
    response = client.post("/api/customers", json={"phone": phone, "name": name})
    assert response.status_code == 200
    return response.json()


def test_customers_page_renders_in_both_languages(client: TestClient) -> None:
    english = client.get("/customers")
    arabic = client.get("/customers", params={"lang": "ar"})
    fallback = client.get("/customers", params={"lang": "klingon"})
    assert english.status_code == arabic.status_code == fallback.status_code == 200

    assert '<html lang="en" dir="ltr">' in english.text
    assert '<html lang="ar" dir="rtl">' in arabic.text
    assert '<html lang="en" dir="ltr">' in fallback.text

    assert "Customer list" in english.text
    assert "Add customer" in english.text
    assert "قائمة العملاء" in arabic.text
    assert "إضافة عميل" in arabic.text

    for page in (english.text, arabic.text):
        assert 'href="/customers"' in page
        assert 'id="customer-form"' in page
        assert 'id="new-phone"' in page and 'id="new-name"' in page
        assert 'id="customers-body"' in page
        assert "Noto+Sans+Arabic" in page
        assert "cdn.tailwindcss.com" in page

    # The empty state row is shown before any customer exists.
    assert 'id="customers-empty"' in english.text
    assert 'id="customers-empty"' in arabic.text


def test_add_customer_is_listed_in_both_languages(client: TestClient) -> None:
    customer = create_customer(client, AHMAD_PHONE, "Ahmad")
    assert customer["balance_cents"] == 0

    english = client.get("/customers")
    arabic = client.get("/customers", params={"lang": "ar"})
    for page, heading in ((english.text, "Customer list"), (arabic.text, "قائمة العملاء")):
        assert 'data-name="Ahmad">' in page
        assert 'data-field="name">Ahmad</td>' in page
        assert AHMAD_PHONE in page
        assert f'data-customer-id="{customer["id"]}"' in page
        assert heading in page
    assert 'id="customers-empty"' not in english.text

    listing = client.get("/api/customers").json()
    assert listing == [{**customer, "debt_cents": 0}]


def test_duplicate_phone_is_rejected(client: TestClient) -> None:
    create_customer(client, AHMAD_PHONE, "Ahmad")
    duplicate = client.post("/api/customers", json={"phone": AHMAD_PHONE, "name": "Rami"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Customer phone already exists"

    # Normalized forms of the same number are duplicates too.
    normalized = client.post("/api/customers", json={"phone": "+963 933-123-456", "name": "Rami"})
    assert normalized.status_code == 409

    listing = client.get("/api/customers").json()
    assert [(item["phone"], item["name"]) for item in listing] == [(AHMAD_PHONE, "Ahmad")]


@pytest.mark.parametrize("phone", ["0923123456", "09331234", "not-a-phone", "123#*1"])
def test_invalid_phone_is_rejected(client: TestClient, phone: str) -> None:
    response = client.post("/api/customers", json={"phone": phone, "name": "Rami"})
    assert response.status_code == 422
    assert "Invalid Syrian mobile number" in response.json()["detail"][0]["msg"]
    assert client.get("/api/customers").json() == []
    assert 'id="customers-empty"' in client.get("/customers").text


def test_edit_customer_name(client: TestClient) -> None:
    customer = create_customer(client, RAMI_PHONE, "Rami")
    response = client.put(f"/api/customers/{customer['id']}", json={"name": "Rami K."})
    assert response.status_code == 200
    assert response.json() == {**customer, "name": "Rami K."}
    assert client.get("/api/customers/lookup", params={"phone": RAMI_PHONE}).json() == response.json()

    page = client.get("/customers")
    assert "Rami K." in page.text
    assert 'data-name="Rami K."' in page.text

    trimmed = client.put(f"/api/customers/{customer['id']}", json={"name": "  Lina  "})
    assert trimmed.status_code == 200
    assert trimmed.json()["name"] == "Lina"

    assert client.put(f"/api/customers/{customer['id']}", json={"name": "   "}).status_code == 422
    assert client.put(f"/api/customers/{customer['id']}", json={"name": ""}).status_code == 422
    assert client.put("/api/customers/999999", json={"name": "Nobody"}).status_code == 404
    assert client.get("/api/customers/lookup", params={"phone": RAMI_PHONE}).json()["name"] == "Lina"


def test_remove_customer(client: TestClient) -> None:
    customer = create_customer(client, AHMAD_PHONE, "Ahmad")
    client.post(
        "/api/debt/charge",
        json={"customer_phone": AHMAD_PHONE, "amount_cents": 15000, "description": "Airtime"},
    )
    transaction = client.post(
        "/api/transactions",
        json={
            "customer_phone": AHMAD_PHONE,
            "operator_code": "mtn",
            "service_code": "prepaid",
            "amount_cents": 5000,
        },
    )
    assert transaction.status_code == 200

    # The customer row owns its ledger and transaction history, so both go with it.
    response = client.delete(f"/api/customers/{customer['id']}")
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/customers/lookup", params={"phone": AHMAD_PHONE}).status_code == 404
    assert client.get("/api/customers").json() == []
    assert 'id="customers-empty"' in client.get("/customers").text

    # Deleting again (or a missing customer) reports 404.
    assert client.delete(f"/api/customers/{customer['id']}").status_code == 404
    assert client.delete("/api/customers/999999").status_code == 404


def test_debt_balance_comes_from_the_ledger(client: TestClient) -> None:
    ahmad = create_customer(client, AHMAD_PHONE, "Ahmad")
    rami = create_customer(client, RAMI_PHONE, "Rami")

    client.post(
        "/api/debt/charge",
        json={"customer_phone": AHMAD_PHONE, "amount_cents": 20000, "description": "Airtime"},
    )
    client.post(
        "/api/debt/charge",
        json={"customer_phone": AHMAD_PHONE, "amount_cents": 5000, "description": "Top-up"},
    )
    client.post(
        "/api/debt/payment",
        json={"customer_phone": AHMAD_PHONE, "amount_cents": 7000, "description": "Partial payment"},
    )

    listing = {item["id"]: item for item in client.get("/api/customers").json()}
    assert listing[ahmad["id"]]["debt_cents"] == 18000
    assert listing[rami["id"]]["debt_cents"] == 0
    assert client.get("/api/debt/summary", params={"customer_phone": AHMAD_PHONE}).json()[
        "total_debt_cents"
    ] == 18000

    english = client.get("/customers").text
    arabic = client.get("/customers", params={"lang": "ar"}).text
    for page, currency in ((english, "SYP"), (arabic, "ل.س")):
        assert "18,000" in page
        assert 'id="total-debt"' in page
        assert page.count('data-field="debt"') == 2
        assert currency in page

    # The debt cell of the customer without ledger entries stays at zero.
    assert 'data-field="debt"><span dir="ltr">0</span>' in english
    assert 'id="total-debt" dir="ltr">18,000</span>' in english


def test_balance_cents_is_listed(client: TestClient) -> None:
    customer = create_customer(client, AHMAD_PHONE, "Ahmad")

    async def set_balance() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            stored = await session.scalar(select(Customer).where(Customer.id == customer["id"]))
            assert stored is not None
            stored.balance_cents = 2500
            await session.commit()

    asyncio.run(set_balance())

    assert client.get("/api/customers").json()[0]["balance_cents"] == 2500
    page = client.get("/customers").text
    assert 'data-field="balance"><span dir="ltr">2,500</span>' in page
    assert 'id="total-balance" dir="ltr">2,500</span>' in page


def test_customers_page_uses_shared_navigation(client: TestClient) -> None:
    page = client.get("/customers").text
    assert 'aria-label="Pages"' in page
    assert '<a href="/pos"' in page
    assert '<a href="/settlement"' in page
    assert 'href="/customers" class="hover:text-blue-800" aria-current="page"' in page
