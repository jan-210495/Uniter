"""Slice 2, part C: recording the real-world result of a POS transaction.

A transaction is created ``pending``; only the explicit operator confirmation
moves it to ``completed`` or ``failed``. The state machine, the absence of
money movement and the existing reporting surfaces are all covered here.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import (
    Base,
    Customer,
    DebtLedger,
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
    """A test client plus the session factory, for direct ledger checks."""
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


def create_transaction(
    client: TestClient,
    *,
    phone: str = PHONE,
    operator_code: str = "mtn",
    service_code: str = "prepaid",
    amount_cents: int = 5000,
) -> dict[str, Any]:
    response = client.post(
        "/api/transactions",
        json={
            "customer_phone": phone,
            "operator_code": operator_code,
            "service_code": service_code,
            "amount_cents": amount_cents,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def set_status(client: TestClient, transaction_id: int, status: str):
    return client.patch(f"/api/transactions/{transaction_id}/status", json={"status": status})


def ledger_snapshot(sessions: async_sessionmaker, transaction_id: int) -> dict[str, Any]:
    async def snapshot(session: AsyncSession) -> dict[str, Any]:
        transaction = await session.get(Transaction, transaction_id)
        profit = await session.scalar(
            select(ProfitLedger).where(ProfitLedger.transaction_id == transaction_id)
        )
        balance = await session.scalar(select(OperatorBalance).order_by(OperatorBalance.id))
        customer = await session.scalar(select(Customer).where(Customer.phone == PHONE))
        debt_rows = await session.scalars(
            select(DebtLedger).where(DebtLedger.customer_id == customer.id)
        )
        return {
            "status": transaction.status,
            "amount_cents": transaction.amount_cents,
            "reference": transaction.reference,
            "ussd_string": transaction.ussd_string,
            "created_at": transaction.created_at,
            "profit": (profit.amount_cents, profit.commission_cents, profit.profit_cents),
            "balance_cents": balance.balance_cents,
            "customer_balance_cents": customer.balance_cents,
            "customer_debt_rows": len(debt_rows.all()),
        }

    return run_db(sessions, snapshot)


# --- Creation stays pending ------------------------------------------------


def test_new_transaction_is_pending_and_reporting_says_so(client) -> None:
    test_client, _ = client
    fund(test_client)
    transaction = create_transaction(test_client)
    assert transaction["status"] == "pending"

    listed = test_client.get("/api/transactions", params={"status": "pending"}).json()
    assert [row["id"] for row in listed] == [transaction["id"]]
    assert test_client.get("/api/transactions", params={"status": "completed"}).json() == []
    assert "Pending" in test_client.get("/operators/mtn").text
    stats = test_client.get("/api/stats/summary", params={"period": 7}).json()
    counts = {row["status"]: row["count"] for row in stats["by_status"]}
    assert counts == {"pending": 1, "completed": 0, "failed": 0}


# --- Valid transitions -----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "other_status", "english", "arabic"),
    [("completed", "failed", "Completed", "مكتملة"), ("failed", "completed", "Failed", "فاشلة")],
)
def test_pending_can_move_to_a_final_status(client, status, other_status, english, arabic) -> None:
    test_client, _ = client
    fund(test_client)
    transaction = create_transaction(test_client)

    response = set_status(test_client, transaction["id"], status)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == status
    # Nothing about the transaction itself changes except the status.
    assert body["reference"] == transaction["reference"]
    assert body["amount_cents"] == transaction["amount_cents"]
    assert body["ussd_string"] == transaction["ussd_string"]

    assert [row["id"] for row in test_client.get("/api/transactions", params={"status": status}).json()] == [
        transaction["id"]
    ]
    assert test_client.get("/api/transactions", params={"status": other_status}).json() == []
    # Operator page and statistics read the stored status.
    assert english in test_client.get("/operators/mtn").text
    assert arabic in test_client.get("/operators/mtn", params={"lang": "ar"}).text
    stats = test_client.get("/api/stats/summary", params={"period": 7}).json()
    counts = {row["status"]: row["count"] for row in stats["by_status"]}
    assert counts[status] == 1 and counts[other_status] == 0
    volumes = {row["status"]: row["volume_cents"] for row in stats["by_status"]}
    assert volumes[status] == transaction["amount_cents"]


# --- Accounting is untouched ----------------------------------------------


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_recording_a_result_moves_no_money(client, status: str) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=200_000)
    transaction = create_transaction(test_client, amount_cents=5000)
    before = ledger_snapshot(sessions, transaction["id"])

    assert set_status(test_client, transaction["id"], status).status_code == 200
    after = ledger_snapshot(sessions, transaction["id"])
    assert after.pop("status") == status
    before.pop("status")
    assert after == before
    # The creation-time deduction and commission stay exactly as recorded.
    assert after["balance_cents"] == 200_000 - 5000
    assert after["profit"] == (5000, 250, 250)
    assert after["customer_balance_cents"] == 0
    assert after["customer_debt_rows"] == 0


def test_a_rejected_transition_changes_nothing(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=200_000)
    transaction = create_transaction(test_client, amount_cents=5000)
    assert set_status(test_client, transaction["id"], "completed").status_code == 200
    settled = ledger_snapshot(sessions, transaction["id"])
    assert set_status(test_client, transaction["id"], "failed").status_code == 409
    assert ledger_snapshot(sessions, transaction["id"]) == settled


def test_a_failed_charge_is_not_refunded_automatically(client) -> None:
    """Documented open business decision: no implicit refund on failure."""
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    transaction = create_transaction(test_client, amount_cents=5000)
    assert set_status(test_client, transaction["id"], "failed").status_code == 200
    balance = {
        row["operator_code"]: row["balance_cents"]
        for row in test_client.get("/api/operators/balance").json()
    }
    assert balance["mtn"] == 95_000
    assert ledger_snapshot(sessions, transaction["id"])["profit"] == (5000, 250, 250)


def test_register_summary_counts_only_completed_transactions(client) -> None:
    test_client, _ = client
    fund(test_client)
    assert test_client.post("/api/register/open", json={"opening_balance_cents": 0}).status_code == 200
    completed = create_transaction(test_client, amount_cents=5000)
    failed = create_transaction(test_client, amount_cents=7000)
    assert set_status(test_client, completed["id"], "completed").status_code == 200
    assert set_status(test_client, failed["id"], "failed").status_code == 200
    summary = test_client.get("/api/register/summary").json()
    assert summary["cash_collected_cents"] == 5000
    assert summary["transaction_count"] == 1


# --- Invalid transitions ---------------------------------------------------


def test_a_final_status_cannot_change_again(client) -> None:
    test_client, _ = client
    fund(test_client)
    transaction = create_transaction(test_client)
    assert set_status(test_client, transaction["id"], "completed").status_code == 200

    for target in ("failed", "completed"):
        response = set_status(test_client, transaction["id"], target)
        assert response.status_code == 409
        assert response.json()["detail"] == "Transaction is already completed"
    assert [row["status"] for row in test_client.get("/api/transactions").json()] == ["completed"]


def test_failed_transaction_cannot_be_completed_later(client) -> None:
    test_client, _ = client
    fund(test_client)
    transaction = create_transaction(test_client)
    assert set_status(test_client, transaction["id"], "failed").status_code == 200
    assert set_status(test_client, transaction["id"], "completed").status_code == 409
    assert test_client.get("/api/transactions").json()[0]["status"] == "failed"


@pytest.mark.parametrize("target", ["pending", "done", "COMPLETED", "", "completed ", "null"])
def test_arbitrary_status_strings_are_rejected(client, target: str) -> None:
    test_client, _ = client
    fund(test_client)
    transaction = create_transaction(test_client)
    response = test_client.patch(
        f"/api/transactions/{transaction['id']}/status", json={"status": target}
    )
    assert response.status_code == 422
    assert test_client.get("/api/transactions").json()[0]["status"] == "pending"


def test_missing_status_field_is_rejected(client) -> None:
    test_client, _ = client
    fund(test_client)
    transaction = create_transaction(test_client)
    response = test_client.patch(f"/api/transactions/{transaction['id']}/status", json={})
    assert response.status_code == 422
    assert test_client.get("/api/transactions").json()[0]["status"] == "pending"


def test_missing_transaction_returns_404(client) -> None:
    test_client, _ = client
    response = set_status(test_client, 9999, "completed")
    assert response.status_code == 404
    assert response.json()["detail"] == "Transaction not found"


@pytest.mark.parametrize("transaction_id", [0, -1])
def test_invalid_transaction_id_is_rejected(client, transaction_id: int) -> None:
    test_client, _ = client
    assert set_status(test_client, transaction_id, "completed").status_code == 422


def test_non_numeric_transaction_id_is_rejected(client) -> None:
    test_client, _ = client
    response = test_client.patch("/api/transactions/not-an-id/status", json={"status": "completed"})
    assert response.status_code == 422


def test_status_update_requires_a_json_body(client) -> None:
    """A cross-site HTML form cannot perform this PATCH/JSON update."""
    test_client, _ = client
    fund(test_client)
    transaction = create_transaction(test_client)
    response = test_client.patch(
        f"/api/transactions/{transaction['id']}/status", data={"status": "completed"}
    )
    assert response.status_code == 422
    assert test_client.get("/api/transactions").json()[0]["status"] == "pending"


# --- Duplicate and concurrent requests ------------------------------------


def test_duplicate_requests_only_apply_once(client) -> None:
    test_client, sessions = client
    fund(test_client)
    transaction = create_transaction(test_client)
    assert set_status(test_client, transaction["id"], "completed").status_code == 200
    assert set_status(test_client, transaction["id"], "completed").status_code == 409

    async def count(session: AsyncSession) -> int:
        rows = await session.scalars(
            select(ProfitLedger).where(ProfitLedger.transaction_id == transaction["id"])
        )
        return len(rows.all())

    assert run_db(sessions, count) == 1


def test_concurrent_updates_have_exactly_one_winner(client) -> None:
    test_client, sessions = client
    fund(test_client)
    transaction = create_transaction(test_client)

    async def attempt(status: str) -> int:
        async with sessions() as session:
            result = await session.execute(
                update(Transaction)
                .where(Transaction.id == transaction["id"], Transaction.status == "pending")
                .values(status=status)
            )
            await session.commit()
            return result.rowcount

    async def race() -> list[int]:
        return sorted(await asyncio.gather(attempt("completed"), attempt("failed")))

    assert asyncio.run(race()) == [0, 1]
    final = test_client.get("/api/transactions").json()[0]["status"]
    assert final in {"completed", "failed"}
    assert ledger_snapshot(sessions, transaction["id"])["profit"] == (5000, 250, 250)
