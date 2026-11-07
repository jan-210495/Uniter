"""Slice 2.3, part A/B: preparing a code, and committing the sale once.

Generating a USSD code is a preview: no row, no debit, no commission. The
seller's Copy or dialer tap commits the sale through ``POST /api/transactions``
with the preview's commit key, which makes the commit idempotent and atomic.
"""

import asyncio
import itertools
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import (
    Base,
    Customer,
    OperatorBalance,
    ProfitLedger,
    Transaction,
    enable_sqlite_foreign_keys,
    get_db,
)
from backend.main import app
from backend.seed import seed_data

PHONE = "0944123456"
CODE = "*150*65477*0944123456*50#"


@pytest.fixture
def client(tmp_path) -> Iterator[tuple[TestClient, async_sessionmaker]]:
    """A test client plus the session factory, for direct ledger checks."""
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "life.db")))
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
            yield test_client, sessions
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


def run_db(sessions: async_sessionmaker, coroutine_factory):
    async def runner():
        async with sessions() as session:
            return await coroutine_factory(session)

    return asyncio.run(runner())


def fund(client: TestClient, operator_code: str = "mtn", amount_cents: int = 5_000_000) -> None:
    assert (
        client.post(
            "/api/operators/topup",
            json={"operator_code": operator_code, "amount_cents": amount_cents},
        ).status_code
        == 200
    )


def balance(sessions: async_sessionmaker, operator_code: str = "mtn") -> int:
    async def read(session: AsyncSession) -> int:
        from backend.database import Operator

        value = await session.scalar(
            select(OperatorBalance.balance_cents)
            .join(Operator, OperatorBalance.operator_id == Operator.id)
            .where(Operator.code == operator_code)
        )
        return int(value or 0)

    return run_db(sessions, read)


def counts(sessions: async_sessionmaker) -> tuple[int, int, int]:
    """(transactions, profit ledger rows, customers)."""

    async def read(session: AsyncSession):
        return (
            await session.scalar(select(func.count()).select_from(Transaction)),
            await session.scalar(select(func.count()).select_from(ProfitLedger)),
            await session.scalar(select(func.count()).select_from(Customer)),
        )

    return run_db(sessions, read)


def ledger_rows(sessions: async_sessionmaker) -> list[dict]:
    async def read(session: AsyncSession):
        rows = (await session.execute(select(ProfitLedger).order_by(ProfitLedger.id))).scalars()
        return [
            {
                "transaction_id": row.transaction_id,
                "operator_code": row.operator_code,
                "amount_cents": row.amount_cents,
                "commission_cents": row.commission_cents,
                "profit_cents": row.profit_cents,
            }
            for row in rows
        ]

    return run_db(sessions, read)


COMMIT_KEYS = itertools.count(1)


def commit_key() -> str:
    return f"commitkey{next(COMMIT_KEYS):023d}"


def preview(
    client: TestClient,
    *,
    phone: str = PHONE,
    operator_code: str = "mtn",
    service_code: str = "prepaid",
    amount_cents: int = 5000,
):
    return client.post(
        "/api/transactions/preview",
        json={
            "customer_phone": phone,
            "operator_code": operator_code,
            "service_code": service_code,
            "amount_cents": amount_cents,
        },
    )


def commit(
    client: TestClient,
    *,
    key: str | None = None,
    phone: str = PHONE,
    operator_code: str = "mtn",
    service_code: str = "prepaid",
    amount_cents: int = 5000,
    **extra,
):
    body = {
        "customer_phone": phone,
        "operator_code": operator_code,
        "service_code": service_code,
        "amount_cents": amount_cents,
        "commit_key": commit_key() if key is None else key,
    }
    body.update(extra)
    return client.post("/api/transactions", json=body)


# --- A. Preparing a code records and charges nothing ----------------------


def test_preview_returns_a_code_and_a_commit_key_without_persisting(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=5_000_000)
    before = balance(sessions)

    response = preview(test_client)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ussd_string"] == CODE
    assert len(payload["commit_key"]) >= 16

    assert counts(sessions) == (0, 0, 0)
    assert balance(sessions) == before
    assert test_client.get("/api/transactions").json() == []
    assert test_client.get("/api/stats/summary").json()["totals"]["transaction_count"] == 0


def test_preview_leaves_no_orphan_after_a_refresh(client) -> None:
    test_client, sessions = client
    fund(test_client)
    for _ in range(3):
        assert preview(test_client).status_code == 200
        # A refresh re-renders the page; nothing was stored by the previews.
        assert test_client.get("/pos").status_code == 200
    assert counts(sessions) == (0, 0, 0)
    assert test_client.get("/api/transactions").json() == []


def test_preview_requires_a_valid_phone_and_amount(client) -> None:
    test_client, sessions = client
    fund(test_client)
    assert preview(test_client, phone="0923123456").status_code == 422
    assert preview(test_client, amount_cents=-1).status_code == 422
    assert counts(sessions) == (0, 0, 0)


def test_preview_rejects_an_unknown_operator_or_service(client) -> None:
    test_client, sessions = client
    fund(test_client)
    assert preview(test_client, operator_code="unknown").status_code == 404
    assert preview(test_client, service_code="unknown").status_code == 404
    assert counts(sessions) == (0, 0, 0)


def test_preview_rejects_insufficient_operator_balance(client) -> None:
    test_client, sessions = client
    response = preview(test_client, amount_cents=50_000)
    assert response.status_code == 400
    assert response.json()["detail"] == "Insufficient operator balance"
    assert counts(sessions) == (0, 0, 0)


# --- B. The commit applies accounting exactly once ------------------------


def test_commit_records_one_successful_sale_and_one_debit(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    key = commit_key()

    response = commit(test_client, key=key)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "successful"
    assert payload["ussd_string"] == CODE
    assert payload["amount_cents"] == 5000
    assert payload["reference"].startswith("TXN-")
    assert payload["created_at"]

    assert counts(sessions) == (1, 1, 1)
    # 100,000 funded, 5,000 charged: exactly one debit.
    assert balance(sessions) == 95_000
    assert ledger_rows(sessions) == [
        {
            "transaction_id": payload["id"],
            "operator_code": "mtn",
            "amount_cents": 5000,
            "commission_cents": 250,
            "profit_cents": 250,
        }
    ]


def test_commit_requires_a_commit_key(client) -> None:
    test_client, sessions = client
    fund(test_client)
    response = test_client.post(
        "/api/transactions",
        json={
            "customer_phone": PHONE,
            "operator_code": "mtn",
            "service_code": "prepaid",
            "amount_cents": 5000,
        },
    )
    assert response.status_code == 422
    assert counts(sessions) == (0, 0, 0)


@pytest.mark.parametrize(
    "key",
    [
        "short",  # too short
        "k" * 65,  # too long
        "bad key with spaces",  # illegal characters
        "../../etc/passwd",  # still illegal characters
        "",
    ],
)
def test_commit_rejects_malformed_keys(client, key: str) -> None:
    test_client, sessions = client
    fund(test_client)
    assert commit(test_client, key=key).status_code == 422
    assert counts(sessions) == (0, 0, 0)


def test_replay_returns_the_same_sale_without_recharging(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    key = commit_key()

    first = commit(test_client, key=key)
    assert first.status_code == 200
    for _ in range(3):
        replay = commit(test_client, key=key)
        assert replay.status_code == 200
        assert replay.json() == first.json()

    assert counts(sessions) == (1, 1, 1)
    assert balance(sessions) == 95_000
    assert len(ledger_rows(sessions)) == 1


def test_replay_with_different_details_is_rejected(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    key = commit_key()
    assert commit(test_client, key=key).status_code == 200

    assert commit(test_client, key=key, amount_cents=7000).status_code == 409
    assert commit(test_client, key=key, phone="0933123456").status_code == 409
    assert commit(test_client, key=key, operator_code="syriatel").status_code == 409

    assert counts(sessions) == (1, 1, 1)
    assert balance(sessions) == 95_000
    assert balance(sessions, "syriatel") == 0


def test_distinct_keys_are_distinct_sales(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)

    first = commit(test_client)
    second = commit(test_client)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["reference"] != second.json()["reference"]

    assert counts(sessions) == (2, 2, 1)
    assert balance(sessions) == 90_000
    assert [row["transaction_id"] for row in ledger_rows(sessions)] == [
        first.json()["id"],
        second.json()["id"],
    ]


def test_insufficient_balance_rejects_atomically(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=1_000)
    key = commit_key()

    response = commit(test_client, key=key, amount_cents=5000)
    assert response.status_code == 400
    assert response.json()["detail"] == "Insufficient operator balance"
    # No transaction, no ledger row, no customer, and the balance is untouched.
    assert counts(sessions) == (0, 0, 0)
    assert balance(sessions) == 1_000
    assert test_client.get("/api/transactions").json() == []

    # The key holds no residue: after funding, the same key commits normally.
    fund(test_client, amount_cents=100_000)
    retry = commit(test_client, key=key, amount_cents=5000)
    assert retry.status_code == 200
    assert retry.json()["status"] == "successful"
    assert counts(sessions) == (1, 1, 1)
    assert balance(sessions) == 96_000


def test_commit_rejects_unknown_configuration_without_customer_side_effects(client) -> None:
    test_client, sessions = client
    fund(test_client)
    assert commit(test_client, operator_code="unknown").status_code == 404
    assert commit(test_client, service_code="unknown").status_code == 404
    assert test_client.get("/api/customers/lookup", params={"phone": PHONE}).status_code == 404
    assert counts(sessions) == (0, 0, 0)
    assert balance(sessions) == 5_000_000


def test_commit_key_uniqueness_is_enforced_by_the_database(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    assert commit(test_client, key="uniquekey000000000001").status_code == 200

    async def duplicate(session: AsyncSession) -> None:
        service_id = (await session.execute(select(Transaction.service_id))).scalar_one()
        customer_id = (await session.execute(select(Transaction.customer_id))).scalar_one()
        session.add(
            Transaction(
                customer_id=customer_id,
                service_id=service_id,
                amount_cents=1,
                status="successful",
                ussd_string="*1#",
                reference="TXN-manual",
                idempotency_key="uniquekey000000000001",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    run_db(sessions, duplicate)
    assert counts(sessions) == (1, 1, 1)


def test_concurrent_identical_commits_charge_once(client) -> None:
    """Two real concurrent requests with one key must produce one sale."""
    from concurrent.futures import ThreadPoolExecutor

    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    key = "racekey00000000000000001"

    def fire(_index: int):
        return commit(test_client, key=key)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = [future.result() for future in [pool.submit(fire, index) for index in range(4)]]

    assert all(response.status_code == 200 for response in responses), [
        response.status_code for response in responses
    ]
    references = {response.json()["reference"] for response in responses}
    assert len(references) == 1
    assert counts(sessions) == (1, 1, 1)
    assert balance(sessions) == 95_000
    assert len(ledger_rows(sessions)) == 1


def test_commit_is_reflected_in_register_and_operator_balance(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    assert test_client.post("/api/register/open", json={"opening_balance_cents": 0}).status_code == 200

    assert commit(test_client).status_code == 200
    summary = test_client.get("/api/register/summary").json()
    assert summary["cash_collected_cents"] == 5000
    assert summary["transaction_count"] == 1
    assert summary["closing_balance_cents"] is None  # the register is still open

    page = test_client.get("/operators/mtn").text
    assert "Successful" in page
    assert 'id="operator-balance" dir="ltr">950</span>' in page
