import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, Customer, Operator, Service, Transaction, enable_sqlite_foreign_keys, get_db
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
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


def test_customer_creation_lookup_and_duplicates(client: TestClient) -> None:
    assert client.get("/api/customers/lookup", params={"phone": "0933123456"}).status_code == 404
    response = client.post("/api/customers", json={"phone": "0933123456", "name": "Customer"})
    assert response.status_code == 200
    customer = response.json()
    assert customer == {"id": customer["id"], "phone": "0933123456", "name": "Customer", "balance_cents": 0}
    assert client.get("/api/customers/lookup", params={"phone": "0933123456"}).json() == customer
    assert client.post("/api/customers", json={"phone": "0933123456", "name": "Duplicate"}).status_code == 409
    assert client.post("/api/customers", json={"phone": "123#*1", "name": "Bad"}).status_code == 422


def test_transaction_existing_and_new_customers(client: TestClient) -> None:
    customer = client.post("/api/customers", json={"phone": "0933123456", "name": "Customer"}).json()
    payload = {"customer_phone": "0933123456", "operator_code": "mtn", "service_code": "prepaid", "amount_cents": 5000}
    response = client.post("/api/transactions", json=payload)
    assert response.status_code == 200
    result = response.json()
    assert result["ussd_string"] == "10050*0933123456#"
    assert result["status"] == "pending" and result["amount_cents"] == 5000
    assert result["reference"].startswith("TXN-") and result["created_at"]
    assert client.get("/api/customers/lookup", params={"phone": "0933123456"}).json() == customer

    payload.update(customer_phone="0944123456", operator_code="syriatel", service_code="postpaid", amount_cents=0)
    response2 = client.post("/api/transactions", json=payload)
    assert response2.status_code == 200
    assert response2.json()["ussd_string"] == "5550*0944123456#"
    assert response2.json()["reference"] != result["reference"]
    created = client.get("/api/customers/lookup", params={"phone": "0944123456"}).json()
    assert created["name"] == "0944123456" and created["balance_cents"] == 0

    async def check_persisted() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            rows = (await session.execute(
                select(Transaction, Customer.phone, Operator.code, Service.code)
                .join(Customer).join(Service).join(Operator).order_by(Transaction.id)
            )).all()
            assert len(rows) == 2
            assert [(row.phone, row[2], row[3]) for row in rows] == [
                ("0933123456", "mtn", "prepaid"), ("0944123456", "syriatel", "postpaid")
            ]
            assert [row.Transaction.ussd_string for row in rows] == [
                "10050*0933123456#", "5550*0944123456#"
            ]

    asyncio.run(check_persisted())


def test_invalid_transaction_has_no_customer_side_effect(client: TestClient) -> None:
    payload = {"customer_phone": "0999999999", "operator_code": "unknown", "service_code": "prepaid", "amount_cents": 5000}
    assert client.post("/api/transactions", json=payload).status_code == 404
    payload["operator_code"] = "mtn"
    payload["service_code"] = "unknown"
    assert client.post("/api/transactions", json=payload).status_code == 404
    payload["service_code"] = "prepaid"
    payload["amount_cents"] = -1
    assert client.post("/api/transactions", json=payload).status_code == 422
    payload["amount_cents"] = 5000
    payload["customer_phone"] = "0999#*1"
    assert client.post("/api/transactions", json=payload).status_code == 422
    assert client.get("/api/customers/lookup", params={"phone": "0999999999"}).status_code == 404


def test_inactive_configuration_is_not_used(client: TestClient) -> None:
    async def disable_service() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            service = await session.scalar(
                select(Service).join(Operator).where(Operator.code == "mtn", Service.code == "prepaid")
            )
            service.is_active = False
            await session.commit()

    asyncio.run(disable_service())
    payload = {"customer_phone": "0999999999", "operator_code": "mtn", "service_code": "prepaid", "amount_cents": 5000}
    assert client.post("/api/transactions", json=payload).status_code == 400
    assert client.get("/api/customers/lookup", params={"phone": "0999999999"}).status_code == 404


def test_failed_transaction_does_not_leave_new_customer(client: TestClient, monkeypatch) -> None:
    from uuid import UUID

    monkeypatch.setattr("backend.main.uuid4", lambda: UUID(int=1))
    payload = {"customer_phone": "0933123456", "operator_code": "mtn", "service_code": "prepaid", "amount_cents": 5000}
    assert client.post("/api/transactions", json=payload).status_code == 200
    payload["customer_phone"] = "0944123456"
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        client.post("/api/transactions", json=payload)
    assert client.get("/api/customers/lookup", params={"phone": "0944123456"}).status_code == 404


def test_normalized_customer_and_transaction_phones(client: TestClient) -> None:
    response = client.post("/api/customers", json={"phone": "+963 933-123-456", "name": "Customer"})
    assert response.status_code == 200
    customer = response.json()
    assert customer["phone"] == "0933123456"
    for phone in ("0933123456", "+963933123456", "00963 933-123-456"):
        lookup = client.get("/api/customers/lookup", params={"phone": phone})
        assert lookup.status_code == 200
        assert lookup.json() == customer
    duplicate = client.post("/api/customers", json={"phone": "00963933123456", "name": "Duplicate"})
    assert duplicate.status_code == 409

    payload = {"customer_phone": "+963933123456", "operator_code": "mtn",
               "service_code": "prepaid", "amount_cents": 5000}
    transaction = client.post("/api/transactions", json=payload)
    assert transaction.status_code == 200
    assert transaction.json()["ussd_string"] == "10050*0933123456#"
    assert client.get("/api/customers/lookup", params={"phone": "0933123456"}).json() == customer

    payload["customer_phone"] = "00963 944-123-456"
    transaction = client.post("/api/transactions", json=payload)
    assert transaction.status_code == 200
    assert transaction.json()["ussd_string"] == "10050*0944123456#"
    created = client.get("/api/customers/lookup", params={"phone": "0944123456"})
    assert created.status_code == 200
    assert created.json()["phone"] == "0944123456"


def test_phone_validation_errors(client: TestClient) -> None:
    response = client.get("/api/customers/lookup", params={"phone": "0923123456"})
    assert response.status_code == 422
    assert "Invalid Syrian mobile number" in response.json()["detail"]
    response = client.post("/api/customers", json={"phone": "0923123456", "name": "Customer"})
    assert response.status_code == 422
    assert "Invalid Syrian mobile number" in response.json()["detail"][0]["msg"]
    payload = {"customer_phone": "0923123456", "operator_code": "mtn",
               "service_code": "prepaid", "amount_cents": 5000}
    response = client.post("/api/transactions", json=payload)
    assert response.status_code == 422
    assert "Invalid Syrian mobile number" in response.json()["detail"][0]["msg"]


def test_commission_settings_and_calculation(client: TestClient) -> None:
    assert client.get("/api/settings/commission").json() == {"commission_rate": "0.05"}
    params = {"amount_cents": 5000, "operator_code": "mtn", "service_code": "prepaid"}
    response = client.get("/api/commission/calculate", params=params)
    assert response.status_code == 200
    assert response.json() == {"amount_cents": 5000, "commission_cents": 250, "net_cents": 4750, "rate": "0.05"}
    update = client.put("/api/settings/commission", json={"rate": "0.12"})
    assert update.status_code == 200
    assert update.json() == {"commission_rate": "0.12"}
    assert client.get("/api/settings/commission").json() == {"commission_rate": "0.12"}
    response = client.get("/api/commission/calculate", params=params)
    assert response.json() == {"amount_cents": 5000, "commission_cents": 600, "net_cents": 4400, "rate": "0.12"}
    params.update(operator_code="syriatel", service_code="postpaid")
    assert client.get("/api/commission/calculate", params=params).json()["commission_cents"] == 600
    params["operator_code"] = "unknown"
    assert client.get("/api/commission/calculate", params=params).status_code == 404
    params.update(operator_code="mtn", service_code="unknown")
    assert client.get("/api/commission/calculate", params=params).status_code == 404
    params.update(service_code="prepaid", amount_cents=-1)
    assert client.get("/api/commission/calculate", params=params).status_code == 422


@pytest.mark.parametrize("rate", ["-0.01", "1.01", "NaN", "Infinity", "-Infinity", "not a number"])
def test_invalid_commission_setting(client: TestClient, rate: str) -> None:
    assert client.put("/api/settings/commission", json={"rate": rate}).status_code == 422
    assert client.get("/api/settings/commission").json() == {"commission_rate": "0.05"}


def test_customer_search_by_phone(client: TestClient) -> None:
    client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"})
    client.post("/api/customers", json={"phone": "0944123456", "name": "Rami"})
    client.post("/api/customers", json={"phone": "0955123456", "name": "Leila"})

    response = client.get("/api/customers/search", params={"q": "0933"})
    assert response.status_code == 200
    results = response.json()
    assert len(results) == 1
    assert results[0]["phone"] == "0933123456"
    assert results[0]["name"] == "Ahmad"


def test_customer_search_by_name(client: TestClient) -> None:
    client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"})
    client.post("/api/customers", json={"phone": "0944123456", "name": "Rami"})
    client.post("/api/customers", json={"phone": "0955123456", "name": "Amira"})

    response = client.get("/api/customers/search", params={"q": "ami"})
    assert response.status_code == 200
    results = response.json()
    names = {r["name"] for r in results}
    assert names == {"Rami", "Amira"}


def test_customer_search_case_insensitive(client: TestClient) -> None:
    client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"})

    response = client.get("/api/customers/search", params={"q": "AHMAD"})
    assert response.status_code == 200
    results = response.json()
    assert len(results) == 1
    assert results[0]["name"] == "Ahmad"


def test_customer_search_includes_debt_and_balance(client: TestClient) -> None:
    customer = client.post(
        "/api/customers", json={"phone": "0933123456", "name": "Ahmad"}
    ).json()
    client.post(
        "/api/customers/1/debt",
        json={"amount_cents": 5000, "description": "charge", "type": "charge"},
    )

    response = client.get("/api/customers/search", params={"q": "Ahmad"})
    assert response.status_code == 200
    result = response.json()[0]
    assert result["balance_cents"] == customer["balance_cents"]
    assert result["debt_cents"] == 5000


def test_customer_search_empty_query_rejected(client: TestClient) -> None:
    assert client.get("/api/customers/search", params={"q": ""}).status_code == 422


def test_customer_search_no_match(client: TestClient) -> None:
    client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"})
    response = client.get("/api/customers/search", params={"q": "0999"})
    assert response.status_code == 200
    assert response.json() == []


def test_customer_search_limit(client: TestClient) -> None:
    for i in range(25):
        client.post(
            "/api/customers",
            json={"phone": f"0933{i:06d}", "name": f"Customer {i}"},
        )
    response = client.get("/api/customers/search", params={"q": "0933"})
    assert response.status_code == 200
    assert len(response.json()) == 20


def test_customer_search_returns_list_item_shape(client: TestClient) -> None:
    """The suggestion dropdown relies on the exact CustomerListItem fields."""
    created = client.post(
        "/api/customers", json={"phone": "0933123456", "name": "Ahmad"}
    ).json()
    client.post(
        f"/api/customers/{created['id']}/debt",
        json={"amount_cents": 5000, "description": "charge", "type": "charge"},
    )
    client.post(
        f"/api/customers/{created['id']}/debt",
        json={"amount_cents": 2000, "description": "payment", "type": "payment"},
    )

    response = client.get("/api/customers/search", params={"q": "Ahmad"})
    assert response.status_code == 200
    results = response.json()
    assert len(results) == 1
    assert set(results[0]) == {"id", "phone", "name", "balance_cents", "debt_cents"}
    assert results[0]["phone"] == "0933123456"
    assert results[0]["debt_cents"] == 3000
