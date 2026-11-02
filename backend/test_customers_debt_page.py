"""Tests for customer debt management and operator balances on /customers."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import (
    Base,
    DebtLedger,
    enable_sqlite_foreign_keys,
    get_db,
)
from backend.main import app
from backend.seed import seed_data


def make_phone(prefix: str, serial: int) -> str:
    """Build a synthetic Syrian mobile number so tests never embed real ones."""
    return f"{prefix}{serial:07d}"


AHMAD_PHONE = make_phone("093", 1)
RAMI_PHONE = make_phone("094", 2)


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(tmp_path / "customers_debt.db"))
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


def add_debt(
    client: TestClient,
    customer_id: int,
    amount_cents: int,
    description: str,
    entry_type: str = "charge",
) -> dict:
    response = client.post(
        f"/api/customers/{customer_id}/debt",
        json={"amount_cents": amount_cents, "description": description, "type": entry_type},
    )
    assert response.status_code == 200
    return response.json()


def balance_of(client: TestClient, customer_id: int) -> dict:
    response = client.get(f"/api/customers/{customer_id}/balance")
    assert response.status_code == 200
    return response.json()


def test_add_debt_charge_for_customer(client: TestClient) -> None:
    ahmad = create_customer(client, AHMAD_PHONE, "Ahmad")

    entry = add_debt(client, ahmad["id"], 15000, "Airtime")
    assert entry["customer_phone"] == AHMAD_PHONE
    assert entry["amount_cents"] == 15000
    assert entry["description"] == "Airtime"
    assert entry["type"] == "charge"
    assert entry["id"] > 0 and entry["created_at"]

    assert balance_of(client, ahmad["id"]) == {
        "total_debt_cents": 15000,
        "total_charges_cents": 15000,
        "total_payments_cents": 0,
    }
    ledger = client.get("/api/debt/ledger", params={"customer_phone": AHMAD_PHONE}).json()
    assert [item["id"] for item in ledger] == [entry["id"]]
    assert client.get("/api/customers").json()[0]["debt_cents"] == 15000


def test_add_debt_payment_for_customer(client: TestClient) -> None:
    ahmad = create_customer(client, AHMAD_PHONE, "Ahmad")
    register = client.post("/api/register/open", json={"opening_balance_cents": 0})
    assert register.status_code == 200

    add_debt(client, ahmad["id"], 20000, "Airtime")
    payment = add_debt(client, ahmad["id"], 8000, "Partial payment", "payment")
    assert payment["type"] == "payment"

    assert balance_of(client, ahmad["id"]) == {
        "total_debt_cents": 12000,
        "total_charges_cents": 20000,
        "total_payments_cents": 8000,
    }
    summary = client.get("/api/debt/summary", params={"customer_phone": AHMAD_PHONE}).json()
    assert summary["total_debt_cents"] == 12000

    # Payments mirror /api/debt/payment and attach to the open register.
    async def register_id_for(entry_id: int) -> int | None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            return await session.scalar(
                select(DebtLedger.register_id).where(DebtLedger.id == entry_id)
            )

    assert asyncio.run(register_id_for(payment["id"])) == register.json()["id"]


def test_edit_debt_entry(client: TestClient) -> None:
    ahmad = create_customer(client, AHMAD_PHONE, "Ahmad")
    entry = add_debt(client, ahmad["id"], 15000, "Airtime")
    url = f"/api/customers/{ahmad['id']}/debt/{entry['id']}"

    edited = client.put(
        url, json={"amount_cents": 18000, "description": "Airtime bundle", "type": "charge"}
    )
    assert edited.status_code == 200
    assert edited.json() == {
        **entry,
        "amount_cents": 18000,
        "description": "Airtime bundle",
    }
    assert balance_of(client, ahmad["id"])["total_debt_cents"] == 18000

    # Fields left out of the body are preserved.
    partial = client.put(url, json={"description": "Correction"})
    assert partial.status_code == 200
    assert partial.json()["amount_cents"] == 18000
    assert partial.json()["type"] == "charge"
    assert partial.json()["description"] == "Correction"

    # Changing the type moves the amount between charges and payments.
    flipped = client.put(url, json={"type": "payment"})
    assert flipped.status_code == 200
    assert balance_of(client, ahmad["id"]) == {
        "total_debt_cents": -18000,
        "total_charges_cents": 0,
        "total_payments_cents": 18000,
    }

    assert client.put(url, json={"type": "refund"}).status_code == 422
    assert client.put(url, json={"amount_cents": -1}).status_code == 422
    assert client.put(f"/api/customers/{ahmad['id']}/debt/999999", json={"amount_cents": 1}).status_code == 404
    assert client.put("/api/customers/999999/debt/1", json={"amount_cents": 1}).status_code == 404


def test_delete_debt_entry(client: TestClient) -> None:
    ahmad = create_customer(client, AHMAD_PHONE, "Ahmad")
    rami = create_customer(client, RAMI_PHONE, "Rami")
    kept = add_debt(client, ahmad["id"], 5000, "Kept charge")
    dropped = add_debt(client, ahmad["id"], 7000, "Dropped charge")
    other = add_debt(client, rami["id"], 9000, "Rami charge")

    response = client.delete(f"/api/customers/{ahmad['id']}/debt/{dropped['id']}")
    assert response.status_code == 204
    assert response.content == b""

    ledger = client.get("/api/debt/ledger", params={"customer_phone": AHMAD_PHONE}).json()
    assert [item["id"] for item in ledger] == [kept["id"]]
    assert balance_of(client, ahmad["id"])["total_debt_cents"] == 5000

    # An entry can only be deleted through its own customer.
    assert client.delete(f"/api/customers/{ahmad['id']}/debt/{other['id']}").status_code == 404
    assert balance_of(client, rami["id"])["total_debt_cents"] == 9000

    assert client.delete(f"/api/customers/{ahmad['id']}/debt/{dropped['id']}").status_code == 404
    assert client.delete("/api/customers/999999/debt/1").status_code == 404


def test_customer_balance_endpoint(client: TestClient) -> None:
    ahmad = create_customer(client, AHMAD_PHONE, "Ahmad")

    assert balance_of(client, ahmad["id"]) == {
        "total_debt_cents": 0,
        "total_charges_cents": 0,
        "total_payments_cents": 0,
    }
    assert set(balance_of(client, ahmad["id"])) == {
        "total_debt_cents",
        "total_charges_cents",
        "total_payments_cents",
    }

    add_debt(client, ahmad["id"], 12000, "Airtime")
    add_debt(client, ahmad["id"], 2000, "Payment", "payment")
    assert balance_of(client, ahmad["id"]) == {
        "total_debt_cents": 10000,
        "total_charges_cents": 12000,
        "total_payments_cents": 2000,
    }
    assert client.get("/api/customers/999999/balance").status_code == 404


def test_existing_phone_based_debt_endpoints_still_work(client: TestClient) -> None:
    ahmad = create_customer(client, AHMAD_PHONE, "Ahmad")
    charge = client.post(
        "/api/debt/charge",
        json={"customer_phone": AHMAD_PHONE, "amount_cents": 6000, "description": "Legacy charge"},
    )
    assert charge.status_code == 200
    payment = client.post(
        "/api/debt/payment",
        json={"customer_phone": AHMAD_PHONE, "amount_cents": 1000, "description": "Legacy payment"},
    )
    assert payment.status_code == 200

    assert balance_of(client, ahmad["id"]) == {
        "total_debt_cents": 5000,
        "total_charges_cents": 6000,
        "total_payments_cents": 1000,
    }
    assert client.get("/api/customers").json()[0]["debt_cents"] == 5000


def test_customers_page_renders_debt_ui_in_both_languages(client: TestClient) -> None:
    ahmad = create_customer(client, AHMAD_PHONE, "Ahmad")
    add_debt(client, ahmad["id"], 15000, "Airtime")

    english = client.get("/customers")
    arabic = client.get("/customers", params={"lang": "ar"})
    assert english.status_code == arabic.status_code == 200
    assert '<html lang="en" dir="ltr">' in english.text
    assert '<html lang="ar" dir="rtl">' in arabic.text

    for page in (english.text, arabic.text):
        for element_id in (
            "debt-modal",
            "debt-form",
            "debt-amount",
            "debt-type",
            "debt-description",
            "debt-entries",
            "debt-total-charges",
            "debt-total-payments",
            "debt-total-debt",
            "operator-balance-title",
            "refresh-operators",
        ):
            assert f'id="{element_id}"' in page
        assert 'data-operator="mtn"' in page
        assert 'data-operator="syriatel"' in page
        assert page.count('data-topup-form novalidate') == 2
        assert 'href="/customers"' in page
        assert 'aria-current="page"' in page
        assert "Noto+Sans+Arabic" in page

    for english_text in ("Add entry", "Operator balances", "Top up", "Debt balance", "Ahmad"):
        assert english_text in english.text
    for arabic_text in ("إضافة قيد", "أرصدة المشغّلين", "شحن الرصيد", "الدين المستحق", "Ahmad"):
        assert arabic_text in arabic.text

    # The list keeps its documented column order.
    headers = english.text.split("<thead", 1)[1].split("</thead>", 1)[0]
    positions = [headers.index(label) for label in ("Name", "Phone", "Balance", "Debt", "Actions")]
    assert positions == sorted(positions)


def test_customer_rows_show_balance_and_debt_balances(client: TestClient) -> None:
    ahmad = create_customer(client, AHMAD_PHONE, "Ahmad")
    rami = create_customer(client, RAMI_PHONE, "Rami")
    add_debt(client, ahmad["id"], 25000, "Airtime")
    add_debt(client, ahmad["id"], 5000, "Payment", "payment")

    page = client.get("/customers").text
    assert 'data-name="Ahmad"' in page
    assert 'data-name="Rami"' in page
    assert "20,000" in page  # Ahmad's aggregated debt inside his row
    assert 'id="total-debt" dir="ltr">20,000</span>' in page

    listing = {item["name"]: item for item in client.get("/api/customers").json()}
    assert listing["Ahmad"]["debt_cents"] == 20000
    assert listing["Rami"]["debt_cents"] == 0


def test_operator_balance_and_topup(client: TestClient) -> None:
    for code, name in (("mtn", "MTN"), ("syriatel", "Syriatel")):
        response = client.get("/api/operators/balance", params={"operator_code": code})
        assert response.status_code == 200
        assert response.json() == {
            "operator_code": code,
            "operator_name": name,
            "balance_cents": 0,
        }

    first = client.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 50000})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["operator_code"] == "mtn"
    assert first_body["amount_cents"] == 50000
    assert first_body["new_balance_cents"] == 50000
    assert first_body["reference"].startswith("TOPUP-")

    second = client.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 25000})
    assert second.status_code == 200
    assert second.json()["new_balance_cents"] == 75000
    assert second.json()["reference"] != first_body["reference"]

    balance = client.get("/api/operators/balance", params={"operator_code": "mtn"}).json()
    assert balance["balance_cents"] == 75000
    syriatel = client.get("/api/operators/balance", params={"operator_code": "syriatel"}).json()
    assert syriatel["balance_cents"] == 0

    assert client.get("/api/operators/balance", params={"operator_code": "unknown"}).status_code == 404
    assert client.post(
        "/api/operators/topup", json={"operator_code": "unknown", "amount_cents": 1000}
    ).status_code == 404
    for amount in (0, -2500):
        assert client.post(
            "/api/operators/topup", json={"operator_code": "mtn", "amount_cents": amount}
        ).status_code == 422
    assert client.get("/api/operators/balance").status_code == 422
