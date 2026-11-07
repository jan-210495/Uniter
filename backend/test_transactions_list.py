import itertools
import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, Transaction, enable_sqlite_foreign_keys, get_db
from backend.main import app
from backend.seed import seed_data


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "tx.db")))
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


def post_transaction(
    client: TestClient, phone: str, operator: str, service: str, amount_cents: int
) -> dict:
    response = client.post(
        "/api/transactions",
        json={
            "customer_phone": phone,
            "operator_code": operator,
            "service_code": service,
            "amount_cents": amount_cents,
            "commit_key": new_commit_key(),
        },
    )
    assert response.status_code == 200
    return response.json()


def mark_failed(transaction_id: int) -> None:
    """The only transition the app allows: a committed sale that failed."""
    async def update() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            transaction = await session.get(Transaction, transaction_id)
            assert transaction is not None
            transaction.status = "failed"
            await session.commit()

    asyncio.run(update())


@pytest.fixture
def transactions(client: TestClient) -> dict[str, dict]:
    """Five committed transactions across both customers and both operators.

    Ahmad: mtn/prepaid x2 and syriatel/postpaid (all successful).
    Rami: syriatel/prepaid successful, mtn/postpaid marked failed.
    """
    for operator_code in ("mtn", "syriatel"):
        assert client.post(
            "/api/operators/topup", json={"operator_code": operator_code, "amount_cents": 1000000}
        ).status_code == 200
    client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"})
    client.post("/api/customers", json={"phone": "0944123456", "name": "Rami"})
    created = {
        "ahmad_mtn_a": post_transaction(client, "0933123456", "mtn", "prepaid", 5000),
        "ahmad_mtn_b": post_transaction(client, "0933123456", "mtn", "prepaid", 2500),
        "ahmad_syriatel": post_transaction(client, "0933123456", "syriatel", "postpaid", 10000),
        "rami_syriatel": post_transaction(client, "0944123456", "syriatel", "prepaid", 1500),
        "rami_mtn": post_transaction(client, "0944123456", "mtn", "postpaid", 0),
    }
    mark_failed(created["rami_mtn"]["id"])
    return created


def ids_of(items: list[dict]) -> set[int]:
    return {item["id"] for item in items}


def get_ids(client: TestClient, **params) -> set[int]:
    response = client.get("/api/transactions", params=params)
    assert response.status_code == 200
    return ids_of(response.json())


def test_list_without_filters_returns_all_transactions(
    client: TestClient, transactions: dict[str, dict]
) -> None:
    response = client.get("/api/transactions")
    assert response.status_code == 200
    items = response.json()
    expected = ids_of(transactions.values())
    assert ids_of(items) == expected
    # Newest first (id breaks created_at ties deterministically).
    assert [item["id"] for item in items] == sorted(expected, reverse=True)

    item = next(i for i in items if i["id"] == transactions["ahmad_mtn_a"]["id"])
    assert set(item) == {
        "id", "reference", "ussd_string", "status", "amount_cents", "created_at",
        "customer_phone", "customer_name", "operator_code", "operator_name",
        "service_code", "service_name",
    }
    assert item["reference"] == transactions["ahmad_mtn_a"]["reference"]
    assert item["ussd_string"] == "*150*65477*0933123456*50#"
    assert item["status"] == "successful" and item["amount_cents"] == 5000
    assert item["created_at"]
    assert item["customer_phone"] == "0933123456" and item["customer_name"] == "Ahmad"
    assert item["operator_code"] == "mtn" and item["operator_name"] == "MTN"
    assert item["service_code"] == "prepaid" and item["service_name"] == "Pre-Paid Charging"


def test_list_filters_by_operator(client: TestClient, transactions: dict[str, dict]) -> None:
    assert get_ids(client, operator_code="mtn") == {
        transactions["ahmad_mtn_a"]["id"],
        transactions["ahmad_mtn_b"]["id"],
        transactions["rami_mtn"]["id"],
    }
    assert get_ids(client, operator_code="syriatel") == {
        transactions["ahmad_syriatel"]["id"],
        transactions["rami_syriatel"]["id"],
    }
    items = client.get("/api/transactions", params={"operator_code": "mtn"}).json()
    assert all(item["operator_code"] == "mtn" and item["operator_name"] == "MTN" for item in items)


def test_list_filters_by_service(client: TestClient, transactions: dict[str, dict]) -> None:
    assert get_ids(client, service_code="prepaid") == {
        transactions["ahmad_mtn_a"]["id"],
        transactions["ahmad_mtn_b"]["id"],
        transactions["rami_syriatel"]["id"],
    }
    assert get_ids(client, service_code="postpaid") == {
        transactions["ahmad_syriatel"]["id"],
        transactions["rami_mtn"]["id"],
    }


def test_list_filters_by_status(client: TestClient, transactions: dict[str, dict]) -> None:
    assert get_ids(client, status="successful") == {
        transactions["ahmad_mtn_a"]["id"],
        transactions["ahmad_mtn_b"]["id"],
        transactions["ahmad_syriatel"]["id"],
        transactions["rami_syriatel"]["id"],
    }
    assert get_ids(client, status="failed") == {transactions["rami_mtn"]["id"]}


def test_list_filters_by_customer_phone(client: TestClient, transactions: dict[str, dict]) -> None:
    assert get_ids(client, customer_phone="0933") == {
        transactions["ahmad_mtn_a"]["id"],
        transactions["ahmad_mtn_b"]["id"],
        transactions["ahmad_syriatel"]["id"],
    }
    # A fragment shared by both customer numbers matches everything.
    assert get_ids(client, customer_phone="12345") == ids_of(transactions.values())
    assert get_ids(client, customer_phone="4412") == {
        transactions["rami_syriatel"]["id"],
        transactions["rami_mtn"]["id"],
    }


def test_list_returns_empty_when_no_match(client: TestClient, transactions: dict[str, dict]) -> None:
    assert client.get("/api/transactions", params={"customer_phone": "0999"}).json() == []
    assert client.get("/api/transactions", params={"operator_code": "unknown"}).json() == []
    assert client.get("/api/transactions", params={"service_code": "unknown"}).json() == []
    assert get_ids(client, operator_code="mtn", status="failed") == {
        transactions["rami_mtn"]["id"]
    }
    assert get_ids(client, operator_code="syriatel", status="failed") == set()


def test_list_combines_filters(client: TestClient, transactions: dict[str, dict]) -> None:
    assert get_ids(client, operator_code="mtn", service_code="prepaid", status="successful",
                   customer_phone="0933") == {
        transactions["ahmad_mtn_a"]["id"],
        transactions["ahmad_mtn_b"]["id"],
    }
    assert get_ids(client, operator_code="syriatel", service_code="prepaid") == {
        transactions["rami_syriatel"]["id"]
    }
    assert get_ids(client, status="successful", customer_phone="0944") == {
        transactions["rami_syriatel"]["id"]
    }


def test_list_rejects_invalid_status(client: TestClient) -> None:
    for status in ("bogus", "pending", "completed"):
        assert client.get("/api/transactions", params={"status": status}).status_code == 422


def test_transactions_page_renders_in_both_languages(client: TestClient) -> None:
    english = client.get("/transactions")
    arabic = client.get("/transactions", params={"lang": "ar"})
    fallback = client.get("/transactions", params={"lang": "invalid"})
    assert english.status_code == arabic.status_code == fallback.status_code == 200
    assert "Transactions" in english.text
    assert '<html lang="ar" dir="rtl">' in arabic.text
    assert "المعاملات" in arabic.text
    assert '<html lang="en" dir="ltr">' in fallback.text
    # Shared navigation exposes the new page and highlights it.
    assert 'href="/transactions"' in english.text
    assert 'aria-current="page"' in english.text


def test_transactions_template_markup() -> None:
    environment = Environment(loader=FileSystemLoader("backend/templates"), autoescape=True)
    template = environment.get_template("transactions.html")
    english = template.render(lang="en", nav_page="transactions")
    arabic = template.render(lang="ar", nav_page="transactions")
    for rendered in (english, arabic):
        # Filter card with the slice-2 suggestion combobox on the phone filter.
        assert 'id="tx-filters"' in rendered
        assert 'id="filter-operator"' in rendered
        assert 'id="filter-service"' in rendered
        assert 'id="filter-status"' in rendered
        assert 'id="filter-phone"' in rendered
        assert 'role="combobox"' in rendered
        assert 'role="listbox"' in rendered
        assert 'aria-expanded="false"' in rendered
        assert 'aria-controls="tx-customer-suggestions"' in rendered
        assert 'aria-autocomplete="list"' in rendered
        # Debounced, abortable client-side fetches.
        assert "const DEBOUNCE_MS = 220;" in rendered
        assert "new AbortController()" in rendered
        assert "fetch('/api/transactions' + buildQuery()" in rendered
        assert "fetch('/api/customers/search?q=' + encodeURIComponent(query)" in rendered
        # Nine table columns: the Slice 2.3 actions column was added.
        assert rendered.count('scope="col"') == 9
        assert 'id="tx-action-status" role="status" aria-live="polite"' in rendered
        assert 'id="tx-table-title" tabindex="-1"' in rendered
    for header in ("Reference", "Date/time", "Customer", "Operator", "Service",
                   "Amount", "Status", "USSD", "Actions"):
        assert header in english
    for header in ("المرجع", "التاريخ والوقت", "العميل", "المشغّل", "الخدمة",
                   "المبلغ", "الحالة", "USSD", "إجراءات"):
        assert header in arabic
    # Status vocabulary and the two row actions.
    assert ">Successful<" in english and ">Failed<" in english
    assert 'value="successful"' in english and 'value="failed"' in english
    assert "pending" not in english.split("<table", 1)[1].split("</table>", 1)[0]
    assert "Mark charge failed" in english and '"remove"' in english
    assert ">ناجحة<" in arabic and ">فاشلة<" in arabic
    assert '"remove"' in arabic and '"markFailed"' in arabic


_COMMIT_KEYS = itertools.count(1)


def new_commit_key() -> str:
    """A fresh commit key: every call is a deliberate, distinct sale."""
    return f"listkey{next(_COMMIT_KEYS):023d}"
