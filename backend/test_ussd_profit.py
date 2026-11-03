import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import (
    Base,
    ProfitLedger,
    Setting,
    Transaction,
    enable_sqlite_foreign_keys,
    get_db,
)
from backend.main import app
from backend.seed import generate_ussd, seed_data


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "ussd.db")))
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


def run_db(statement) -> None:
    """Execute a Core statement against the override session."""

    async def execute() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            await session.execute(statement)
            await session.commit()

    asyncio.run(execute())


def topup(client: TestClient, operator_code: str, amount_cents: int) -> None:
    response = client.post(
        "/api/operators/topup", json={"operator_code": operator_code, "amount_cents": amount_cents}
    )
    assert response.status_code == 200


def post_transaction(
    client: TestClient, phone: str, operator: str, service: str, amount_cents: int
):
    return client.post(
        "/api/transactions",
        json={
            "customer_phone": phone,
            "operator_code": operator,
            "service_code": service,
            "amount_cents": amount_cents,
        },
    )


def test_generate_ussd_exact_structures(client: TestClient) -> None:
    """The four operator/service combinations match the reference structures."""

    async def check() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            assert (
                await generate_ussd(session, "mtn", "prepaid", "0968663410", 5000)
                == "*150*65477*0968663410*500#"
            )
            assert (
                await generate_ussd(session, "mtn", "postpaid", "0968663410", 5000)
                == "*154*65477*0968663410*500#"
            )
            assert (
                await generate_ussd(session, "syriatel", "prepaid", "0968663410", 5000)
                == "*150*1*67858*1*500*0968663410*0968663410#"
            )
            assert (
                await generate_ussd(session, "syriatel", "postpaid", "0968663410", 5000)
                == "*150*1*67858*5*500*0968663410*0968663410#"
            )

    asyncio.run(check())


def test_generate_ussd_uses_stored_domain_settings(client: TestClient) -> None:
    run_db(update(Setting).where(Setting.key == "MTN_DOMAIN").values(value="11111"))
    run_db(update(Setting).where(Setting.key == "SYRIATEL_DOMAIN").values(value="22222"))

    async def check() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            assert (
                await generate_ussd(session, "mtn", "prepaid", "0968663410", 5000)
                == "*150*11111*0968663410*500#"
            )
            assert (
                await generate_ussd(session, "syriatel", "postpaid", "0968663410", 5000)
                == "*150*1*22222*5*500*0968663410*0968663410#"
            )

    asyncio.run(check())


def test_generate_ussd_falls_back_without_stored_domains(client: TestClient) -> None:
    run_db(delete(Setting).where(Setting.key.in_(("MTN_DOMAIN", "SYRIATEL_DOMAIN"))))

    async def check() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            assert (
                await generate_ussd(session, "mtn", "prepaid", "0968663410", 5000)
                == "*150*65477*0968663410*500#"
            )
            assert (
                await generate_ussd(session, "syriatel", "prepaid", "0968663410", 5000)
                == "*150*1*67858*1*500*0968663410*0968663410#"
            )

    asyncio.run(check())


def test_seed_inserts_domain_settings_idempotently(client: TestClient) -> None:
    run_db(update(Setting).where(Setting.key == "MTN_DOMAIN").values(value="99999"))

    async def reseed_and_read() -> dict[str, str]:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            await seed_data(session)
            await seed_data(session)
            rows = (await session.execute(select(Setting.key, Setting.value))).all()
            return {key: value for key, value in rows}

    settings = asyncio.run(reseed_and_read())
    # Existing values are preserved; defaults are not duplicated or reset.
    assert settings["MTN_DOMAIN"] == "99999"
    assert settings["SYRIATEL_DOMAIN"] == "67858"
    assert len(settings) == 2


def test_transaction_deducts_balance_and_records_profit(client: TestClient) -> None:
    topup(client, "mtn", 20000)
    response = post_transaction(client, "0933123456", "mtn", "prepaid", 5000)
    assert response.status_code == 200
    transaction = response.json()
    assert transaction["ussd_string"] == "*150*65477*0933123456*500#"

    balances = {item["operator_code"]: item for item in client.get("/api/operators/balance").json()}
    assert balances["mtn"]["balance_cents"] == 15000
    assert balances["syriatel"]["balance_cents"] == 0

    async def check_profit() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            entry = (
                await session.execute(select(ProfitLedger).where(ProfitLedger.transaction_id.isnot(None)))
            ).scalar_one()
            assert entry.transaction_id == transaction["id"]
            assert entry.operator_code == "mtn"
            assert entry.amount_cents == 5000
            assert entry.commission_cents == 250  # 5% of 5000
            assert entry.profit_cents == 4750
            assert entry.created_at

    asyncio.run(check_profit())


def test_insufficient_balance_rejects_transaction(client: TestClient) -> None:
    # Seeded balances are zero: any positive amount is insufficient.
    response = post_transaction(client, "0933123456", "mtn", "prepaid", 5000)
    assert response.status_code == 400
    assert response.json()["detail"] == "Insufficient operator balance"

    async def check_nothing_created() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            assert (await session.scalars(select(Transaction))).all() == []
            assert (await session.scalars(select(ProfitLedger))).all() == []

    asyncio.run(check_nothing_created())
    # No customer placeholder was left behind and the balance is untouched.
    assert client.get("/api/customers").json() == []
    balances = {item["operator_code"]: item for item in client.get("/api/operators/balance").json()}
    assert balances["mtn"]["balance_cents"] == 0

    # A partial balance below the amount is still insufficient.
    topup(client, "mtn", 4999)
    assert post_transaction(client, "0933123456", "mtn", "prepaid", 5000).status_code == 400
    balances = {item["operator_code"]: item for item in client.get("/api/operators/balance").json()}
    assert balances["mtn"]["balance_cents"] == 4999

    # The full balance allows the transaction through.
    topup(client, "mtn", 1)
    assert post_transaction(client, "0933123456", "mtn", "prepaid", 5000).status_code == 200
    balances = {item["operator_code"]: item for item in client.get("/api/operators/balance").json()}
    assert balances["mtn"]["balance_cents"] == 0


def test_profit_ledger_accumulates_across_transactions(client: TestClient) -> None:
    topup(client, "mtn", 30000)
    topup(client, "syriatel", 20000)

    assert post_transaction(client, "0933123456", "mtn", "prepaid", 5000).status_code == 200
    assert post_transaction(client, "0944123456", "mtn", "prepaid", 2500).status_code == 200
    # A changed commission rate applies to later transactions only.
    assert client.put("/api/settings/commission", json={"rate": "0.10"}).status_code == 200
    assert post_transaction(client, "0933123456", "syriatel", "postpaid", 10000).status_code == 200

    balances = {item["operator_code"]: item for item in client.get("/api/operators/balance").json()}
    assert balances["mtn"]["balance_cents"] == 30000 - 7500
    assert balances["syriatel"]["balance_cents"] == 20000 - 10000

    async def check_ledger() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            entries = (
                await session.execute(
                    select(ProfitLedger).order_by(ProfitLedger.id)
                )
            ).scalars().all()
            assert len(entries) == 3
            assert [(e.operator_code, e.amount_cents, e.commission_cents, e.profit_cents) for e in entries] == [
                ("mtn", 5000, 250, 4750),
                ("mtn", 2500, 125, 2375),
                ("syriatel", 10000, 1000, 9000),
            ]
            assert sum(e.commission_cents for e in entries) == 1375
            assert sum(e.profit_cents for e in entries) == 16125

    asyncio.run(check_ledger())


def test_zero_amount_transaction_needs_no_balance(client: TestClient) -> None:
    response = post_transaction(client, "0933123456", "syriatel", "postpaid", 0)
    assert response.status_code == 200
    assert response.json()["ussd_string"] == "*150*1*67858*5*0*0933123456*0933123456#"
    balances = {item["operator_code"]: item for item in client.get("/api/operators/balance").json()}
    assert balances["syriatel"]["balance_cents"] == 0

    async def check_zero_profit() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            entry = (await session.scalars(select(ProfitLedger))).one()
            assert (entry.amount_cents, entry.commission_cents, entry.profit_cents) == (0, 0, 0)

    asyncio.run(check_zero_profit())
