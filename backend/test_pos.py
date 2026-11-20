import asyncio
import itertools
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, Operator, Service, Transaction, enable_sqlite_foreign_keys, get_db
from backend.auth_test_support import sign_in
from backend.sim_test_support import fund_company, sim_balance, with_sim
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


def test_named_and_unnamed_sales_keep_their_own_phone_and_name(client: TestClient) -> None:
    """The sale's optional name is a snapshot; the phone is always stored."""
    for operator_code in ("mtn", "syriatel"):
        fund_company(client, operator_code, 100000)  # Slice 4.1: a funded SIM card
    payload = {
        "customer_phone": "0933123456",
        "customer_name": "Customer",
        "operator_code": "mtn",
        "service_code": "prepaid",
        "amount_cents": 5000,
    }
    response = post_transaction(client, payload)
    assert response.status_code == 200
    result = response.json()
    assert result["ussd_string"] == "*150*65477*0933123456*50#"
    assert result["status"] == "successful" and result["amount_cents"] == 5000
    assert result["reference"].startswith("TXN-") and result["created_at"]

    # No name: committed and accounted for, but no customer record at all.
    payload.update(
        customer_phone="0944123456", operator_code="syriatel", service_code="postpaid", amount_cents=0
    )
    payload.pop("customer_name")
    response2 = post_transaction(client, payload)
    assert response2.status_code == 200
    assert response2.json()["ussd_string"] == "*150*1*67858*5*0*0944123456*0944123456#"
    assert response2.json()["reference"] != result["reference"]

    async def check_persisted() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            rows = (await session.execute(
                select(Transaction, Operator.code, Service.code)
                .select_from(Transaction)
                .join(Service, Transaction.service_id == Service.id)
                .join(Operator, Service.operator_id == Operator.id)
                .order_by(Transaction.id)
            )).all()
            assert len(rows) == 2
            assert [(row.Transaction.customer_phone, row[1], row[2]) for row in rows] == [
                ("0933123456", "mtn", "prepaid"), ("0944123456", "syriatel", "postpaid")
            ]
            # Only the named sale has a name snapshot.
            assert [row.Transaction.customer_name for row in rows] == ["Customer", None]
            assert [row.Transaction.ussd_string for row in rows] == [
                "*150*65477*0933123456*50#", "*150*1*67858*5*0*0944123456*0944123456#"
            ]

    asyncio.run(check_persisted())


def test_invalid_transaction_has_no_customer_side_effect(client: TestClient) -> None:
    payload = {"customer_phone": "0999999999", "operator_code": "unknown", "service_code": "prepaid", "amount_cents": 5000}
    assert post_transaction(client, payload).status_code == 404
    payload["operator_code"] = "mtn"
    payload["service_code"] = "unknown"
    assert post_transaction(client, payload).status_code == 404
    payload["service_code"] = "prepaid"
    payload["amount_cents"] = -1
    assert post_transaction(client, payload).status_code == 422
    payload["amount_cents"] = 5000
    payload["customer_phone"] = "0999#*1"
    assert post_transaction(client, payload).status_code == 422


def test_inactive_configuration_is_not_used(client: TestClient) -> None:
    async def disable_service() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            service = await session.scalar(
                select(Service).join(Operator).where(Operator.code == "mtn", Service.code == "prepaid")
            )
            service.is_active = False
            await session.commit()

    fund_company(client, "mtn", 100000)  # funded: only the inactive service refuses the sale
    asyncio.run(disable_service())
    payload = {"customer_phone": "0999999999", "operator_code": "mtn", "service_code": "prepaid", "amount_cents": 5000}
    assert post_transaction(client, payload).status_code == 400


def test_failed_transaction_does_not_leave_new_customer(client: TestClient, monkeypatch) -> None:
    from uuid import UUID

    monkeypatch.setattr("backend.main.uuid4", lambda: UUID(int=1))
    fund_company(client, "mtn", 25000)  # Slice 4.1: a funded SIM card
    payload = {"customer_phone": "0933123456", "operator_code": "mtn", "service_code": "prepaid", "amount_cents": 5000}
    assert post_transaction(client, payload).status_code == 200
    payload["customer_phone"] = "0944123456"
    # The frozen uuid4 makes the reference collide, so the second commit fails
    # atomically: no customer, no row, no debit, and a retryable 409.
    assert post_transaction(client, payload).status_code == 409
    assert len(client.get("/api/transactions").json()) == 1
    assert sim_balance(client, client.sim_ids["mtn"]) == 20000  # debited once only


def test_normalized_customer_and_transaction_phones(client: TestClient) -> None:
    fund_company(client, "mtn", 100000)  # Slice 4.1: a funded SIM card
    payload = {"customer_phone": "+963933123456", "customer_name": "Customer",
               "operator_code": "mtn", "service_code": "prepaid", "amount_cents": 5000}
    transaction = post_transaction(client, payload)
    assert transaction.status_code == 200
    assert transaction.json()["ussd_string"] == "*150*65477*0933123456*50#"

    payload["customer_phone"] = "00963 944-123-456"
    payload["customer_name"] = "Rami"
    transaction = post_transaction(client, payload)
    assert transaction.status_code == 200
    assert transaction.json()["ussd_string"] == "*150*65477*0944123456*50#"
    assert transaction.json()["customer_name"] == "Rami"


def test_phone_validation_errors(client: TestClient) -> None:
    payload = {"customer_phone": "0923123456", "operator_code": "mtn",
               "service_code": "prepaid", "amount_cents": 5000}
    response = post_transaction(client, payload)
    assert response.status_code == 422
    assert "Invalid Syrian mobile number" in response.json()["detail"][0]["msg"]


def test_commission_settings_and_calculation(client: TestClient) -> None:
    assert client.get("/api/settings/commission").json() == {"commission_rate": "0.05"}
    params = {"amount_cents": 5000, "operator_code": "mtn", "service_code": "prepaid"}
    response = client.get("/api/commission/calculate", params=params)
    assert response.status_code == 200
    assert response.json() == {"amount_cents": 5000, "commission_cents": 250, "net_cents": 4750, "total_cents": 5250, "rate": "0.05"}
    update = client.put("/api/settings/commission", json={"rate": "0.12"})
    assert update.status_code == 200
    assert update.json() == {"commission_rate": "0.12"}
    assert client.get("/api/settings/commission").json() == {"commission_rate": "0.12"}
    response = client.get("/api/commission/calculate", params=params)
    assert response.json() == {"amount_cents": 5000, "commission_cents": 600, "net_cents": 4400, "total_cents": 5600, "rate": "0.12"}
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


_COMMIT_KEYS = itertools.count(1)


def new_commit_key() -> str:
    """A fresh commit key: each call is a deliberate, distinct sale."""
    return f"testposkey{next(_COMMIT_KEYS):019d}"


def post_transaction(client: TestClient, payload: dict, **overrides):
    body = dict(payload)
    body.update(overrides)
    body["commit_key"] = new_commit_key()
    return client.post("/api/transactions", json=with_sim(client, body))
