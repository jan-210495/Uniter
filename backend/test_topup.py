import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import (
    Base,
    Operator,
    OperatorBalance,
    OperatorTopup,
    enable_sqlite_foreign_keys,
    get_db,
)
from backend.main import app
from backend.seed import seed_data


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "topup.db")))
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


def balances_by_code(client: TestClient) -> dict[str, dict]:
    response = client.get("/api/operators/balance")
    assert response.status_code == 200
    return {item["operator_code"]: item for item in response.json()}


def test_balance_list_returns_all_operators(client: TestClient) -> None:
    response = client.get("/api/operators/balance")
    assert response.status_code == 200
    items = response.json()
    assert isinstance(items, list) and len(items) == 2
    assert items == [
        {"operator_code": "mtn", "operator_name": "MTN", "balance_cents": 0},
        {"operator_code": "syriatel", "operator_name": "Syriatel", "balance_cents": 0},
    ]


def test_topup_creates_record_and_updates_balance(client: TestClient) -> None:
    first = client.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 50000})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["operator_code"] == "mtn"
    assert first_body["amount_cents"] == 50000
    assert first_body["new_balance_cents"] == 50000
    assert first_body["reference"].startswith("TOPUP-")

    # The balance update persists for later requests.
    balances = balances_by_code(client)
    assert balances["mtn"]["balance_cents"] == 50000
    assert balances["syriatel"]["balance_cents"] == 0

    # A second top-up accumulates on the same balance.
    second = client.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 25000})
    assert second.status_code == 200
    assert second.json()["new_balance_cents"] == 75000
    assert second.json()["reference"] != first_body["reference"]
    assert balances_by_code(client)["mtn"]["balance_cents"] == 75000

    # The audit trail recorded both entries for the operator.
    async def check_audit() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            rows = (
                await session.execute(
                    select(OperatorTopup, Operator.code)
                    .join(Operator, OperatorTopup.operator_id == Operator.id)
                    .where(Operator.code == "mtn")
                    .order_by(OperatorTopup.id)
                )
            ).all()
            assert [(row[0].amount_cents, row[1]) for row in rows] == [(50000, "mtn"), (25000, "mtn")]
            assert {row[0].reference for row in rows} == {first_body["reference"], second.json()["reference"]}

    asyncio.run(check_audit())


@pytest.mark.parametrize("amount", [-100, -2500])
def test_topup_rejects_negative_amount(client: TestClient, amount: int) -> None:
    response = client.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": amount})
    assert response.status_code == 422
    assert balances_by_code(client)["mtn"]["balance_cents"] == 0


def test_topup_rejects_zero_amount(client: TestClient) -> None:
    response = client.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 0})
    assert response.status_code == 422
    assert balances_by_code(client)["mtn"]["balance_cents"] == 0


def test_topup_rejects_unknown_operator(client: TestClient) -> None:
    response = client.post("/api/operators/topup", json={"operator_code": "unknown", "amount_cents": 1000})
    assert response.status_code == 404
    # Nothing was created for an unknown operator.
    assert len(client.get("/api/operators/balance").json()) == 2


def test_seed_is_idempotent_for_operator_balances(client: TestClient) -> None:
    async def reseed_and_count() -> tuple[int, list[int]]:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            await seed_data(session)
            await seed_data(session)
            balances = (
                await session.scalars(select(OperatorBalance).order_by(OperatorBalance.operator_id))
            ).all()
            return len(balances), [balance.balance_cents for balance in balances]

    count, values = asyncio.run(reseed_and_count())
    # One row per operator, still zero — no duplicates from re-seeding.
    assert count == 2
    assert values == [0, 0]


def test_reseed_preserves_existing_operator_balance(client: TestClient) -> None:
    assert client.post(
        "/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 12345}
    ).status_code == 200

    async def reseed() -> dict[str, int]:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            await seed_data(session)
            rows = (
                await session.execute(
                    select(Operator.code, OperatorBalance.balance_cents).join(
                        OperatorBalance, OperatorBalance.operator_id == Operator.id
                    )
                )
            ).all()
            return {code: balance for code, balance in rows}

    # Re-seeding must not reset credit the shop already holds.
    assert asyncio.run(reseed()) == {"mtn": 12345, "syriatel": 0}


def test_topup_page_renders_in_both_languages(client: TestClient) -> None:
    english = client.get("/topup")
    arabic = client.get("/topup", params={"lang": "ar"})
    fallback = client.get("/topup", params={"lang": "invalid"})
    assert english.status_code == arabic.status_code == fallback.status_code == 200
    assert "Balance top-up" in english.text
    assert "Remaining balance" in english.text
    assert "Last top-up" in english.text
    assert '<html lang="ar" dir="rtl">' in arabic.text
    assert "تعبئة الرصيد" in arabic.text
    assert "الرصيد المتبقي" in arabic.text
    assert "آخر تعبئة" in arabic.text
    assert '<html lang="en" dir="ltr">' in fallback.text
    # Both operator cards with their top-up forms.
    for code in ("mtn", "syriatel"):
        assert f'data-operator-card="{code}"' in english.text
        assert f'data-operator-code="{code}"' in english.text
    # The page records top-ups and refreshes balances through the API.
    assert "fetch('/api/operators/topup'" in english.text
    assert "fetch('/api/operators/balance'" in english.text
    # Shared navigation exposes the new page and highlights it.
    assert 'href="/topup"' in english.text
    assert 'aria-current="page"' in english.text


def test_topup_page_shows_balance_and_last_topup(client: TestClient) -> None:
    topup = client.post(
        "/api/operators/topup", json={"operator_code": "syriatel", "amount_cents": 75000}
    ).json()
    html = client.get("/topup").text
    # The refreshed balance is rendered server-side.
    assert "75,000" in html
    # The last top-up timestamp is exposed for locale formatting.
    assert 'data-field="last-topup"' in html
    assert 'data-iso=' in html
    # The audit reference from the recorded top-up exists in the trail.
    async def check_reference() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            assert await session.scalar(
                select(OperatorTopup).where(OperatorTopup.reference == topup["reference"])
            ) is not None

    asyncio.run(check_reference())
