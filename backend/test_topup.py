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
    SimLedger,
    enable_sqlite_foreign_keys,
    get_db,
)
from backend.auth_test_support import sign_in
from backend.sim_test_support import create_sim, sim_balance, topup_sim
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
            sign_in(test_client, sessions)
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


def test_legacy_topup_is_read_only_and_records_nothing(client: TestClient) -> None:
    """v1.0.4 Slice 4.1: the legacy company credit can no longer be topped up.

    New money goes onto one SIM card at a time (``POST /api/sims/{id}/topup``);
    the old company credit stays exactly as it was, with no new audit row.
    """
    response = client.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 50000})
    assert response.status_code == 409
    assert response.json()["detail"] == "Legacy company credit is read-only; top up a SIM card instead"
    assert balances_by_code(client)["mtn"]["balance_cents"] == 0
    assert balances_by_code(client)["syriatel"]["balance_cents"] == 0

    async def legacy_topups() -> int:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            return len((await session.scalars(select(OperatorTopup))).all())

    assert asyncio.run(legacy_topups()) == 0


def test_sim_topup_creates_record_and_updates_balance(client: TestClient) -> None:
    """The per-SIM replacement keeps the old guarantees: persisted, accumulating, audited."""
    sim_id = create_sim(client, "mtn")["id"]
    first = topup_sim(client, sim_id, 50000)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["sim_id"] == sim_id
    assert first_body["amount_cents"] == 50000
    assert first_body["balance_cents"] == 50000
    assert first_body["reference"].startswith("SIMTOPUP-")
    # The balance update persists for later requests.
    assert sim_balance(client, sim_id) == 50000

    # A second top-up accumulates on the same balance.
    second = topup_sim(client, sim_id, 25000)
    assert second.status_code == 200
    assert second.json()["balance_cents"] == 75000
    assert second.json()["reference"] != first_body["reference"]
    assert sim_balance(client, sim_id) == 75000
    # The legacy company credit is untouched.
    assert balances_by_code(client)["mtn"]["balance_cents"] == 0

    # The audit trail recorded both entries for the SIM card.
    async def check_audit() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            rows = (
                await session.scalars(
                    select(SimLedger).where(SimLedger.sim_card_id == sim_id).order_by(SimLedger.id)
                )
            ).all()
            assert [(row.kind, row.amount_cents, row.balance_after_cents) for row in rows] == [
                ("topup", 50000, 50000), ("topup", 25000, 75000)
            ]
            assert {row.reference for row in rows} == {first_body["reference"], second.json()["reference"]}

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


def _legacy_credit(code: str, amount_cents: int, reference: str = "TOPUP-legacy") -> None:
    """Historical company credit, as a pre-4.1 database holds it."""

    async def write() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            operator = await session.scalar(select(Operator).where(Operator.code == code))
            balance = await session.scalar(
                select(OperatorBalance).where(OperatorBalance.operator_id == operator.id)
            )
            balance.balance_cents += amount_cents
            session.add(OperatorTopup(operator_id=operator.id, amount_cents=amount_cents, reference=reference))
            await session.commit()

    asyncio.run(write())


def test_reseed_preserves_existing_operator_balance(client: TestClient) -> None:
    _legacy_credit("mtn", 12345)

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
    # /topup now leads to the Operators page, where shop credit is topped up.
    assert english.url.path == "/operators"
    # v1.0.4 Slice 4.1: the old shop credit is labelled as legacy company credit.
    assert "Operators" in english.text and "Legacy company credit" in english.text
    assert "Remaining balance" in english.text
    assert "Last top-up" in english.text
    assert '<html lang="ar" dir="rtl">' in arabic.text
    assert "رصيد الشركة القديم" in arabic.text
    assert "الرصيد المتبقي" in arabic.text
    assert "آخر تعبئة" in arabic.text
    assert '<html lang="en" dir="ltr">' in fallback.text
    # Both operator cards; the legacy credit is read-only (no legacy top-up form).
    for code in ("mtn", "syriatel"):
        assert f'data-operator-card="{code}"' in english.text
    assert "fetch('/api/operators/topup'" not in english.text
    # Top-ups are recorded per SIM card through the API.
    assert "'/api/sims/' + encodeURIComponent(form.dataset.simId) + '/topup'" in english.text
    # Shared navigation exposes the new page and highlights it.
    assert 'href="/operators"' in english.text
    assert 'aria-current="page"' in english.text


def test_topup_page_shows_balance_and_last_topup(client: TestClient) -> None:
    # Historical (pre-4.1) company credit is still shown, read-only.
    _legacy_credit("syriatel", 75000)
    topup = {"reference": "TOPUP-legacy"}
    html = client.get("/topup").text
    # The refreshed balance is rendered server-side.
    assert 'data-field="balance" dir="ltr">750</span>' in html  # 75000 cents = 750 SYP
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
