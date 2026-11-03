import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, Customer, enable_sqlite_foreign_keys, get_db
from backend.main import app
from backend.seed import seed_data


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(tmp_path / "recognition.db"))
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


def stored_customer(phone: str) -> Customer | None:
    async def fetch() -> Customer | None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            return await session.scalar(select(Customer).where(Customer.phone == phone))

    return asyncio.run(fetch())


def test_lookup_by_name_exact_match(client: TestClient) -> None:
    created = client.post(
        "/api/customers", json={"phone": "0933123456", "name": "Ahmad"}
    ).json()

    response = client.get("/api/customers/lookup-by-name", params={"name": "Ahmad"})
    assert response.status_code == 200
    assert response.json() == {
        "id": created["id"],
        "phone": "0933123456",
        "name": "Ahmad",
        "balance_cents": 0,
    }

    # Case-insensitive exact match; fragments do not match.
    assert client.get("/api/customers/lookup-by-name", params={"name": "ahmad"}).status_code == 200
    assert client.get("/api/customers/lookup-by-name", params={"name": "AHMAD"}).status_code == 200
    assert client.get("/api/customers/lookup-by-name", params={"name": "Ahm"}).status_code == 404
    assert client.get("/api/customers/lookup-by-name", params={"name": "Rami"}).status_code == 404


def test_lookup_by_name_returns_first_of_duplicates(client: TestClient) -> None:
    first = client.post("/api/customers", json={"phone": "0933123456", "name": "Ahmad"}).json()
    client.post("/api/customers", json={"phone": "0944123456", "name": "Ahmad"})

    response = client.get("/api/customers/lookup-by-name", params={"name": "ahmad"})
    assert response.status_code == 200
    assert response.json()["id"] == first["id"]


def test_lookup_by_name_rejects_empty_name(client: TestClient) -> None:
    assert client.get("/api/customers/lookup-by-name", params={"name": ""}).status_code == 422


def test_create_customer_stores_operator_and_service(client: TestClient) -> None:
    response = client.post(
        "/api/customers",
        json={
            "phone": "0933123456",
            "name": "Ahmad",
            "operator_code": "mtn",
            "service_code": "prepaid",
        },
    )
    assert response.status_code == 200
    # The response shape is unchanged; the preferences are stored on the record.
    assert set(response.json()) == {"id", "phone", "name", "balance_cents"}

    customer = stored_customer("0933123456")
    assert customer is not None
    assert customer.operator_code == "mtn"
    assert customer.service_code == "prepaid"


def test_create_customer_without_optional_fields(client: TestClient) -> None:
    response = client.post(
        "/api/customers", json={"phone": "0933123456", "name": "Ahmad"}
    )
    assert response.status_code == 200
    customer = stored_customer("0933123456")
    assert customer is not None
    assert customer.operator_code is None
    assert customer.service_code is None

    # Each preference can also be stored on its own.
    assert client.post(
        "/api/customers", json={"phone": "0944123456", "name": "Rami", "operator_code": "syriatel"}
    ).status_code == 200
    rami = stored_customer("0944123456")
    assert rami is not None
    assert rami.operator_code == "syriatel" and rami.service_code is None
    assert client.post(
        "/api/customers", json={"phone": "0955123456", "name": "Amira", "service_code": "postpaid"}
    ).status_code == 200
    amira = stored_customer("0955123456")
    assert amira is not None
    assert amira.operator_code is None and amira.service_code == "postpaid"


def test_create_customer_rejects_unknown_preferences(client: TestClient) -> None:
    assert client.post(
        "/api/customers",
        json={"phone": "0933123456", "name": "Ahmad", "operator_code": "nope"},
    ).status_code == 422
    assert client.post(
        "/api/customers",
        json={"phone": "0933123456", "name": "Ahmad", "service_code": "nope"},
    ).status_code == 422

    # A service that exists, but not under the given operator.
    async def add_syriatel_only_service() -> None:
        from sqlalchemy import select as sa_select

        from backend.database import Operator, Service

        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            operator = await session.scalar(sa_select(Operator).where(Operator.code == "syriatel"))
            session.add(
                Service(
                    operator_id=operator.id,
                    name="Syriatel Only",
                    code="syriatel-only",
                    ussd_suffix="#",
                    price_per_unit=Decimal("0.00"),
                )
            )
            await session.commit()

    asyncio.run(add_syriatel_only_service())
    assert client.post(
        "/api/customers",
        json={"phone": "0933123456", "name": "Ahmad", "operator_code": "mtn",
              "service_code": "syriatel-only"},
    ).status_code == 422
    # Without an operator the code alone is still valid.
    assert client.post(
        "/api/customers",
        json={"phone": "0955123456", "name": "Amira", "service_code": "syriatel-only"},
    ).status_code == 200
    assert stored_customer("0933123456") is None


def test_customers_page_add_form_has_operator_and_service_fields(client: TestClient) -> None:
    english = client.get("/customers")
    arabic = client.get("/customers", params={"lang": "ar"})
    assert english.status_code == arabic.status_code == 200
    for rendered in (english.text, arabic.text):
        assert 'id="new-operator"' in rendered
        assert '<option value="mtn">MTN</option>' in rendered
        assert '<option value="syriatel">Syriatel</option>' in rendered
        assert 'id="new-service"' in rendered
        assert 'value="prepaid"' in rendered
        assert 'value="postpaid"' in rendered
    assert "Operator (optional)" in english.text
    assert "Service type (optional)" in english.text
    assert "المشغّل (اختياري)" in arabic.text
    assert "نوع الخدمة (اختياري)" in arabic.text

    # The form submits the optional fields to the API and they are stored.
    response = client.post(
        "/api/customers",
        json={
            "phone": "0933123456",
            "name": "Ahmad",
            "operator_code": "mtn",
            "service_code": "prepaid",
        },
    )
    assert response.status_code == 200
    customer = stored_customer("0933123456")
    assert customer is not None
    assert (customer.operator_code, customer.service_code) == ("mtn", "prepaid")


def test_pos_page_has_auto_operator_and_auto_register(client: TestClient) -> None:
    english = client.get("/pos")
    arabic = client.get("/pos", params={"lang": "ar"})
    assert english.status_code == arabic.status_code == 200
    for rendered in (english.text, arabic.text):
        # Auto-operator prefix map, applied while typing.
        assert "'093': 'mtn'" in rendered
        assert "'098': 'mtn'" in rendered
        assert "'099': 'mtn'" in rendered
        assert "'094': 'syriatel'" in rendered
        assert "'095': 'syriatel'" in rendered
        assert "'096': 'syriatel'" in rendered
        # Auto-register flow: lookup by name, then create with preferences.
        assert "'/api/customers/lookup-by-name?name=' + encodeURIComponent(name)" in rendered
        assert "fetch('/api/customers'" in rendered
        assert "payload.operator_code = operator.value" in rendered
        assert "payload.service_code = service.value" in rendered
        # Feedback live region below the form.
        assert 'id="register-status"' in rendered
        assert 'role="status"' in rendered
    assert "Customer registered:" in english.text
    assert "Customer found:" in english.text
    # Arabic script messages are emitted by tojson as escaped unicode.
    import json

    for message in ("تم تسجيل العميل:", "تم العثور على العميل:"):
        assert json.dumps(message, ensure_ascii=True)[1:-1] in arabic.text
