"""Slice 2.3, part C: marking a sale failed and removing failed rows.

A committed sale is ``successful``. The seller can record the real-world outcome
on the Transactions page (successful -> failed) and may remove failed rows from
the reporting views. Neither action moves money: the operator debit, the
commission and the profit ledger row stay exactly as recorded, which is the
documented no-refund policy (see the handoff).
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import (
    Base,
    Customer,
    Operator,
    OperatorBalance,
    ProfitLedger,
    Transaction,
    enable_sqlite_foreign_keys,
    get_db,
)
from backend.main import app
from backend.seed import seed_data

PHONE = "0944123456"


@pytest.fixture
def client(tmp_path) -> Iterator[tuple[TestClient, async_sessionmaker]]:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "status.db")))
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


_KEYS = 0


def create_transaction(
    client: TestClient,
    *,
    phone: str = PHONE,
    operator_code: str = "mtn",
    service_code: str = "prepaid",
    amount_cents: int = 5000,
    customer_name: str | None = "Ahmad",
) -> dict[str, Any]:
    # Commit keys live on the client side; one per deliberate sale.
    client.commit_keys = getattr(client, "commit_keys", 0) + 1
    response = client.post(
        "/api/transactions",
        json={
            "customer_phone": phone,
            "operator_code": operator_code,
            "service_code": service_code,
            "amount_cents": amount_cents,
            "commit_key": f"statuskey{client.commit_keys:022d}",
            **({"customer_name": customer_name} if customer_name else {}),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def balance(sessions: async_sessionmaker, operator_code: str = "mtn") -> int:
    async def read(session: AsyncSession) -> int:
        value = await session.scalar(
            select(OperatorBalance.balance_cents)
            .join(Operator, OperatorBalance.operator_id == Operator.id)
            .where(Operator.code == operator_code)
        )
        return int(value or 0)

    return run_db(sessions, read)


def profits(sessions: async_sessionmaker) -> list[tuple[int, int, int]]:
    async def read(session: AsyncSession):
        rows = (await session.execute(select(ProfitLedger).order_by(ProfitLedger.id))).scalars()
        return [(row.transaction_id, row.commission_cents, row.profit_cents) for row in rows]

    return run_db(sessions, read)


def raw_row(sessions: async_sessionmaker, transaction_id: int) -> dict | None:
    async def read(session: AsyncSession):
        row = await session.get(Transaction, transaction_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "status": row.status,
            "amount_cents": row.amount_cents,
            "reference": row.reference,
            "ussd_string": row.ussd_string,
            "deleted_at": row.deleted_at,
            "idempotency_key": row.idempotency_key,
        }

    return run_db(sessions, read)


def set_failed(client: TestClient, transaction_id: int):
    return client.patch(f"/api/transactions/{transaction_id}/status", json={"status": "failed"})


# --- Marking a successful sale failed ------------------------------------


def test_marking_successful_as_failed_changes_only_the_status(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    transaction = create_transaction(test_client)
    assert transaction["status"] == "successful"
    profit_before = profits(sessions)
    balance_before = balance(sessions)

    response = set_failed(test_client, transaction["id"])
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["reference"] == transaction["reference"]

    # No money moved and the ledger was not edited.
    assert balance(sessions) == balance_before
    assert profits(sessions) == profit_before
    assert raw_row(sessions, transaction["id"])["status"] == "failed"
    listed = {row["id"]: row["status"] for row in test_client.get("/api/transactions").json()}
    assert listed[transaction["id"]] == "failed"


@pytest.mark.parametrize("payload", [{"status": "successful"}, {"status": "pending"}])
def test_invalid_target_statuses_are_rejected(client, payload: dict) -> None:
    test_client, sessions = client
    fund(test_client)
    transaction = create_transaction(test_client)
    response = test_client.patch(f"/api/transactions/{transaction['id']}/status", json=payload)
    assert response.status_code in (409, 422)
    assert raw_row(sessions, transaction["id"])["status"] == "successful"


@pytest.mark.parametrize("value", ["", "completed", "done", "FAILED", None, 7])
def test_arbitrary_status_strings_are_rejected(client, value) -> None:
    test_client, sessions = client
    fund(test_client)
    transaction = create_transaction(test_client)
    response = test_client.patch(
        f"/api/transactions/{transaction['id']}/status", json={"status": value}
    )
    assert response.status_code == 422
    assert raw_row(sessions, transaction["id"])["status"] == "successful"


def test_marking_an_unknown_transaction_is_a_404(client) -> None:
    test_client, _ = client
    assert set_failed(test_client, 4242).status_code == 404
    assert set_failed(test_client, 0).status_code == 422


def test_failed_is_final(client) -> None:
    test_client, sessions = client
    fund(test_client)
    transaction = create_transaction(test_client)
    assert set_failed(test_client, transaction["id"]).status_code == 200
    # Repeating the action changes nothing and never re-applies accounting.
    assert set_failed(test_client, transaction["id"]).status_code == 409
    assert raw_row(sessions, transaction["id"])["status"] == "failed"
    assert len(profits(sessions)) == 1
    assert balance(sessions) == 5_000_000 - 5000


def test_marking_failed_twice_never_touches_accounting(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    first = create_transaction(test_client, amount_cents=1000)
    second = create_transaction(test_client, amount_cents=2000)
    assert set_failed(test_client, first["id"]).status_code == 200
    balance_after_first = balance(sessions)
    ledger_after_first = profits(sessions)

    assert set_failed(test_client, first["id"]).status_code == 409
    assert set_failed(test_client, second["id"]).status_code == 200

    assert balance(sessions) == balance_after_first
    assert profits(sessions) == ledger_after_first
    assert [row["status"] for row in test_client.get("/api/transactions").json()] == [
        "failed",
        "failed",
    ]


# --- Removing failed rows -------------------------------------------------


def test_successful_transactions_cannot_be_removed(client) -> None:
    test_client, sessions = client
    fund(test_client)
    transaction = create_transaction(test_client)
    response = test_client.delete(f"/api/transactions/{transaction['id']}")
    assert response.status_code == 409
    assert raw_row(sessions, transaction["id"])["deleted_at"] is None
    assert [row["id"] for row in test_client.get("/api/transactions").json()] == [transaction["id"]]


def test_removing_a_failed_transaction_tombstones_it_without_refunding(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    transaction = create_transaction(test_client, amount_cents=5000)
    assert set_failed(test_client, transaction["id"]).status_code == 200
    profit_before = profits(sessions)
    balance_before = balance(sessions)
    customer_before = test_client.get("/api/customers/lookup", params={"phone": PHONE}).json()

    response = test_client.delete(f"/api/transactions/{transaction['id']}")
    assert response.status_code == 204
    assert response.content == b""

    # Hidden from every live view, but the row and its ledger row still exist.
    assert test_client.get("/api/transactions").json() == []
    assert transaction["id"] not in {
        row["id"] for row in test_client.get("/api/search", params={"q": PHONE}).json()["transactions"]
    }
    assert not any(
        transaction["reference"] in row
        for row in [test_client.get("/operators/mtn").text]
    )
    row = raw_row(sessions, transaction["id"])
    assert row["deleted_at"] is not None
    assert row["status"] == "failed"
    assert row["amount_cents"] == 5000
    assert row["idempotency_key"] == f"statuskey{test_client.commit_keys:022d}"

    # No refund, no commission reversal, no customer balance change.
    assert profits(sessions) == profit_before
    assert balance(sessions) == balance_before
    assert test_client.get("/api/customers/lookup", params={"phone": PHONE}).json() == customer_before


def test_removal_is_safe_to_repeat(client) -> None:
    test_client, sessions = client
    fund(test_client)
    transaction = create_transaction(test_client)
    assert set_failed(test_client, transaction["id"]).status_code == 200
    assert test_client.delete(f"/api/transactions/{transaction['id']}").status_code == 204
    tombstone = raw_row(sessions, transaction["id"])["deleted_at"]
    assert test_client.delete(f"/api/transactions/{transaction['id']}").status_code == 204
    # The original removal time is preserved.
    assert raw_row(sessions, transaction["id"])["deleted_at"] == tombstone


def test_removing_an_unknown_transaction_is_a_404(client) -> None:
    test_client, _ = client
    assert test_client.delete("/api/transactions/4242").status_code == 404


def test_removed_failed_rows_still_block_a_replay(client) -> None:
    """Replaying a commit whose sale was removed returns it, never recharges."""
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    transaction = create_transaction(test_client, amount_cents=5000)
    key = raw_row(sessions, transaction["id"])["idempotency_key"]
    assert set_failed(test_client, transaction["id"]).status_code == 200
    assert test_client.delete(f"/api/transactions/{transaction['id']}").status_code == 204
    balance_before = balance(sessions)

    replay = test_client.post(
        "/api/transactions",
        json={
            "customer_phone": PHONE,
            "operator_code": "mtn",
            "service_code": "prepaid",
            "amount_cents": 5000,
            "commit_key": key,
        },
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == transaction["id"]
    assert replay.json()["status"] == "failed"
    assert balance(sessions) == balance_before
    assert len(profits(sessions)) == 1


# --- Reporting consistency ------------------------------------------------


def test_filters_reflect_the_two_persisted_statuses(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=500_000)
    first = create_transaction(test_client, amount_cents=1000)
    second = create_transaction(test_client, amount_cents=2000)
    create_transaction(test_client, amount_cents=3000)
    assert set_failed(test_client, second["id"]).status_code == 200

    successful = test_client.get("/api/transactions", params={"status": "successful"}).json()
    failed = test_client.get("/api/transactions", params={"status": "failed"}).json()
    assert {row["id"] for row in successful} == {
        first["id"],
        # third sale
        sorted(row["id"] for row in test_client.get("/api/transactions").json())[-1],
    }
    assert [row["id"] for row in failed] == [second["id"]]
    for legacy in ("pending", "completed"):
        assert test_client.get("/api/transactions", params={"status": legacy}).status_code == 422

    # Removing the failed one drops it from the failed filter too.
    assert test_client.delete(f"/api/transactions/{second['id']}").status_code == 204
    assert test_client.get("/api/transactions", params={"status": "failed"}).json() == []
    assert set(test_client.get("/api/transactions", params={"status": "successful"}).json()[0]) == set(
        successful[0]
    )


def test_stats_count_failed_sales_but_drop_removed_ones(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=500_000)
    first = create_transaction(test_client, amount_cents=10_000)
    second = create_transaction(test_client, amount_cents=4000)
    third = create_transaction(test_client, amount_cents=1000)

    summary = test_client.get("/api/stats/summary").json()
    assert summary["totals"]["transaction_count"] == 3
    assert summary["totals"]["volume_cents"] == 15_000
    assert summary["totals"]["profit_cents"] == 750  # 500 + 200 + 50
    assert summary["by_status"] == [
        {"status": "successful", "count": 3, "volume_cents": 15_000},
        {"status": "failed", "count": 0, "volume_cents": 0},
    ]

    # A failed sale still counts as a sale that happened.
    assert set_failed(test_client, third["id"]).status_code == 200
    summary = test_client.get("/api/stats/summary").json()
    assert summary["totals"]["transaction_count"] == 3
    assert summary["totals"]["volume_cents"] == 15_000
    assert summary["by_status"] == [
        {"status": "successful", "count": 2, "volume_cents": 14_000},
        {"status": "failed", "count": 1, "volume_cents": 1000},
    ]
    assert summary["totals"]["profit_cents"] == 750  # no automatic reversal

    # Removal is a reporting tombstone: volume and profit both go.
    assert test_client.delete(f"/api/transactions/{third['id']}").status_code == 204
    summary = test_client.get("/api/stats/summary").json()
    assert summary["totals"]["transaction_count"] == 2
    assert summary["totals"]["volume_cents"] == 14_000
    assert summary["totals"]["profit_cents"] == 700
    assert summary["by_status"] == [
        {"status": "successful", "count": 2, "volume_cents": 14_000},
        {"status": "failed", "count": 0, "volume_cents": 0},
    ]
    daily_total = sum(row["transaction_count"] for row in summary["daily"])
    assert daily_total == 2


def test_register_summary_counts_successful_cash_only(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=500_000)
    assert test_client.post("/api/register/open", json={"opening_balance_cents": 0}).status_code == 200
    first = create_transaction(test_client, amount_cents=5000)
    second = create_transaction(test_client, amount_cents=3000)
    create_transaction(test_client, amount_cents=2000)

    summary = test_client.get("/api/register/summary").json()
    assert summary["cash_collected_cents"] == 10_000
    assert summary["transaction_count"] == 3

    assert set_failed(test_client, second["id"]).status_code == 200
    summary = test_client.get("/api/register/summary").json()
    assert summary["cash_collected_cents"] == 7000
    assert summary["transaction_count"] == 2

    assert test_client.delete(f"/api/transactions/{second['id']}").status_code == 204
    assert test_client.get("/api/register/summary").json()["cash_collected_cents"] == 7000
    assert raw_row(sessions, first["id"])["status"] == "successful"


def test_operator_page_shows_both_statuses_in_both_languages(client) -> None:
    test_client, _ = client
    fund(test_client)
    failed = create_transaction(test_client, amount_cents=1000)
    create_transaction(test_client, amount_cents=2000)
    assert set_failed(test_client, failed["id"]).status_code == 200

    english = test_client.get("/operators/mtn").text
    assert "Successful" in english and "Failed" in english
    arabic = test_client.get("/operators/mtn", params={"lang": "ar"}).text
    assert "ناجحة" in arabic and "فاشلة" in arabic

    # Removed rows leave the operator history.
    assert test_client.delete(f"/api/transactions/{failed['id']}").status_code == 204
    english = test_client.get("/operators/mtn").text
    assert "Failed" not in english
    assert "Successful" in english


def test_customer_accounting_is_untouched_by_status_changes(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    transaction = create_transaction(test_client, amount_cents=5000)
    customer = test_client.get("/api/customers/lookup", params={"phone": PHONE}).json()

    assert set_failed(test_client, transaction["id"]).status_code == 200
    assert test_client.get("/api/customers/lookup", params={"phone": PHONE}).json() == customer
    balance_payload = test_client.get(f"/api/customers/{customer['id']}/balance").json()
    assert balance_payload["total_debt_cents"] == 0
    # The customer keeps their (now failed) transaction history.
    customer_transactions = test_client.get(
        "/api/transactions", params={"customer_phone": PHONE}
    ).json()
    assert [row["id"] for row in customer_transactions] == [transaction["id"]]
    assert test_client.delete(f"/api/transactions/{transaction['id']}").status_code == 204
    assert test_client.get("/api/customers/lookup", params={"phone": PHONE}).json() == customer
    assert balance(sessions) == 95_000
