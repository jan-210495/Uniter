import itertools
import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, enable_sqlite_foreign_keys, get_db
from backend.main import app
from backend.seed import seed_data

EMPTY_RESULTS = {"customers": [], "transactions": [], "operators": []}


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "search.db")))
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


def post_transaction(
    client: TestClient,
    phone: str,
    operator: str,
    service: str,
    amount: int,
    customer_name: str | None = None,
) -> dict:
    body = {
        "customer_phone": phone,
        "operator_code": operator,
        "service_code": service,
        "amount_cents": amount,
        "commit_key": new_commit_key(),
    }
    if customer_name is not None:
        body["customer_name"] = customer_name
    response = client.post("/api/transactions", json=body)
    assert response.status_code == 200
    return response.json()


def search(client: TestClient, q: str) -> dict:
    response = client.get("/api/search", params={"q": q})
    assert response.status_code == 200
    return response.json()


def test_search_with_empty_or_missing_query(client: TestClient) -> None:
    assert search(client, "") == EMPTY_RESULTS
    response = client.get("/api/search")
    assert response.status_code == 200
    assert response.json() == EMPTY_RESULTS
    assert search(client, "   ") == EMPTY_RESULTS


def test_search_customer_by_name_fragment(client: TestClient) -> None:
    ahmad = client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"}).json()
    client.post("/api/customers", json={"phone": "0944123456", "name": "Rami"})
    assert client.post(
        f"/api/customers/{ahmad['id']}/debt",
        json={"amount_cents": 5000, "description": "charge", "type": "charge"},
    ).status_code == 200

    results = search(client, "ah")
    assert results["transactions"] == [] and results["operators"] == []
    assert len(results["customers"]) == 1
    customer = results["customers"][0]
    assert set(customer) == {"id", "phone", "name", "balance_cents", "debt_cents"}
    assert customer["name"] == "Ahmad"
    assert customer["phone"] == "0933123456"
    assert customer["debt_cents"] == 5000

    # Case-insensitive and full-name matches work; other names do not.
    assert search(client, "AHMAD")["customers"][0]["name"] == "Ahmad"
    assert search(client, "Rami")["customers"][0]["name"] == "Rami"
    assert search(client, "Ziad")["customers"] == []


def test_search_customer_by_phone_fragment(client: TestClient) -> None:
    client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"})
    client.post("/api/customers", json={"phone": "0944123456", "name": "Rami"})

    results = search(client, "0933")
    assert [customer["name"] for customer in results["customers"]] == ["Ahmad"]
    results = search(client, "4412")
    assert [customer["name"] for customer in results["customers"]] == ["Rami"]
    assert search(client, "0999")["customers"] == []


def test_search_transaction_by_reference(client: TestClient) -> None:
    topup(client, "mtn", 100000)
    client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"})
    transaction = post_transaction(
        client, "0933123456", "mtn", "prepaid", 5000, customer_name="Ahmad"
    )

    results = search(client, transaction["reference"])
    assert results["customers"] == []
    assert results["transactions"][0]["id"] == transaction["id"]
    item = results["transactions"][0]
    assert set(item) == {
        "id", "reference", "amount_cents", "status", "customer_id", "customer_phone",
        "customer_name", "operator_code", "operator_name",
    }
    assert item["amount_cents"] == 5000 and item["status"] == "successful"
    assert item["customer_phone"] == "0933123456" and item["customer_name"] == "Ahmad"
    assert item["operator_code"] == "mtn" and item["operator_name"] == "MTN"

    # A customer phone fragment surfaces their transactions too.
    by_phone = search(client, "0933")
    assert [t["id"] for t in by_phone["transactions"]] == [transaction["id"]]
    assert by_phone["customers"][0]["name"] == "Ahmad"


def test_search_finds_committed_one_time_sales(client: TestClient) -> None:
    """A sale with no customer record is still searchable, by reference and phone."""
    topup(client, "mtn", 100000)
    transaction = post_transaction(client, "0955123456", "mtn", "prepaid", 1200)

    by_reference = search(client, transaction["reference"])
    assert by_reference["customers"] == []
    assert [t["id"] for t in by_reference["transactions"]] == [transaction["id"]]
    item = by_reference["transactions"][0]
    assert item["customer_id"] is None
    assert item["customer_phone"] == "0955123456"
    assert item["customer_name"] is None
    assert item["operator_code"] == "mtn"

    # The row's own phone is matched too, though no customer holds it.
    by_phone = search(client, "0955")
    assert [t["id"] for t in by_phone["transactions"]] == [transaction["id"]]
    assert by_phone["customers"] == []


def test_search_operators_by_code_or_name(client: TestClient) -> None:
    topup(client, "mtn", 50000)

    results = search(client, "mt")
    assert results["operators"] == [{"code": "mtn", "name": "MTN", "balance_cents": 50000}]
    assert search(client, "SYRIA")["operators"] == [
        {"code": "syriatel", "name": "Syriatel", "balance_cents": 0}
    ]
    assert search(client, "unknown")["operators"] == []


def test_search_mixed_query_spans_all_sections(client: TestClient) -> None:
    topup(client, "mtn", 100000)
    client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"})
    client.post("/api/customers", json={"phone": "0944123456", "name": "Lina"})
    post_transaction(client, "0944123456", "mtn", "prepaid", 2500, customer_name="Lina")

    # "n" matches the customer Lina, every TXN- reference and the operator MTN.
    results = search(client, "n")
    assert [customer["name"] for customer in results["customers"]] == ["Lina"]
    assert len(results["transactions"]) == 1
    assert results["transactions"][0]["customer_name"] == "Lina"
    assert [operator["code"] for operator in results["operators"]] == ["mtn"]


def test_search_limits_each_section_to_ten(client: TestClient) -> None:
    for i in range(15):
        client.post(
            "/api/customers",
            json={"phone": f"0933{i:06d}", "name": f"Ahmad {i:02d}"},
        )
    results = search(client, "ahmad")
    assert len(results["customers"]) == 10

    topup(client, "mtn", 100000000)
    for i in range(12):
        post_transaction(client, f"0933{i:06d}", "mtn", "prepaid", 1000)
    references = search(client, "TXN")
    assert len(references["transactions"]) == 10
    # Newest transactions come first.
    ids = [item["id"] for item in references["transactions"]]
    assert ids == sorted(ids, reverse=True)

_COMMIT_KEYS = itertools.count(1)


def new_commit_key() -> str:
    """A fresh commit key: every call is a deliberate, distinct sale."""
    return f"testkey{next(_COMMIT_KEYS):022d}"
