"""POS names are per-transaction snapshots, independent of customer records."""

import asyncio
import itertools
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import (
    Base,
    Customer,
    DebtLedger,
    Operator,
    OperatorBalance,
    ProfitLedger,
    SimBalance,
    SimCard,
    Transaction,
    enable_sqlite_foreign_keys,
    get_db,
)
from backend.auth_test_support import sign_in
from backend.sim_test_support import fund_company, with_sim
from backend.main import app
from backend.seed import seed_data

PHONE = "0944123456"
OTHER_PHONE = "0933123456"
CODE = "*150*65477*0944123456*50#"


@pytest.fixture
def client(tmp_path) -> Iterator[tuple[TestClient, async_sessionmaker]]:
    """A test client plus the session factory, for direct accounting checks."""
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(tmp_path / "customer.db"))
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
            sign_in(test_client, sessions)
            yield test_client, sessions
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


def run_db(sessions: async_sessionmaker, coroutine_factory):
    async def runner():
        async with sessions() as session:
            return await coroutine_factory(session)

    return asyncio.run(runner())


COMMIT_KEYS = itertools.count(1)


def commit_key() -> str:
    return f"customkey{next(COMMIT_KEYS):022d}"


def fund(
    client: TestClient,
    operator_code: str = "mtn",
    amount_cents: int = 5_000_000,
) -> None:
    # Slice 4.1: sales are charged to a SIM card, funded by its own top-up.
    fund_company(client, operator_code, amount_cents)


def screenshot(sessions: async_sessionmaker) -> dict:
    """Everything this slice must hold constant, or move exactly once."""

    async def read(session: AsyncSession):
        return {
            "transactions": await session.scalar(select(func.count()).select_from(Transaction)),
            "ledger": await session.scalar(select(func.count()).select_from(ProfitLedger)),
            "customers": await session.scalar(select(func.count()).select_from(Customer)),
            "debt": await session.scalar(select(func.count()).select_from(DebtLedger)),
            # Slice 4.1: the money a sale moves is the SIM card balance ...
            "balances": {
                row.code: row.balance
                for row in (
                    await session.execute(
                        select(
                            Operator.code,
                            func.coalesce(func.sum(SimBalance.balance_cents), 0).label("balance"),
                        )
                        .outerjoin(SimCard, SimCard.operator_id == Operator.id)
                        .outerjoin(SimBalance, SimBalance.sim_card_id == SimCard.id)
                        .group_by(Operator.code)
                    )
                ).all()
            },
            # ... and the legacy company credit never moves at all.
            "legacy": {
                row.code: row.balance
                for row in (
                    await session.execute(
                        select(
                            Operator.code,
                            func.coalesce(OperatorBalance.balance_cents, 0).label("balance"),
                        ).outerjoin(OperatorBalance, OperatorBalance.operator_id == Operator.id)
                    )
                ).all()
            },
        }

    return run_db(sessions, read)


def preview(
    client: TestClient,
    *,
    phone: str = PHONE,
    name: str | None = None,
    operator_code: str = "mtn",
    service_code: str = "prepaid",
    amount_cents: int = 5000,
):
    body = {
        "customer_phone": phone,
        "operator_code": operator_code,
        "service_code": service_code,
        "amount_cents": amount_cents,
    }
    if name is not None:
        body["customer_name"] = name
    return client.post("/api/transactions/preview", json=with_sim(client, body))


def commit(
    client: TestClient,
    *,
    key: str | None = None,
    phone: str = PHONE,
    name: str | None = None,
    operator_code: str = "mtn",
    service_code: str = "prepaid",
    amount_cents: int = 5000,
    include_name: bool = True,
):
    body = {
        "customer_phone": phone,
        "operator_code": operator_code,
        "service_code": service_code,
        "amount_cents": amount_cents,
        "commit_key": commit_key() if key is None else key,
    }
    if include_name:
        body["customer_name"] = name
    return client.post("/api/transactions", json=with_sim(client, body))


def customers(sessions: async_sessionmaker) -> list[dict]:
    async def read(session: AsyncSession):
        rows = (await session.execute(select(Customer).order_by(Customer.id))).scalars()
        return [
            {
                "id": row.id,
                "phone": row.phone,
                "name": row.name,
                "operator_code": row.operator_code,
                "service_code": row.service_code,
            }
            for row in rows
        ]

    return run_db(sessions, read)


def stored(sessions: async_sessionmaker) -> list[dict]:
    async def read(session: AsyncSession):
        rows = (await session.execute(select(Transaction).order_by(Transaction.id))).scalars()
        return [
            {
                "id": row.id,
                "customer_id": row.customer_id,
                "customer_name": row.customer_name,
                "customer_phone": row.customer_phone,
                "amount_cents": row.amount_cents,
                "status": row.status,
                "reference": row.reference,
            }
            for row in rows
        ]

    return run_db(sessions, read)


# --- 1. Preparing a code writes nothing ------------------------------------


@pytest.mark.parametrize("name", [None, "Ahmad", "", "   ", "\t \n"])
def test_preview_never_creates_a_customer_or_moves_money(client, name: str) -> None:
    test_client, sessions = client
    fund(test_client)
    before = screenshot(sessions)

    response = preview(test_client, name=name)
    assert response.status_code == 200
    assert response.json()["ussd_string"] == CODE
    assert response.json()["commit_key"]

    # No customer, no transaction, no ledger row, no balance movement.
    assert screenshot(sessions) == before
    assert customers(sessions) == []
    assert test_client.get("/api/transactions").json() == []


# --- 2. A named sale stores a snapshot without resolving a customer -------


def test_named_copy_commit_stores_normalized_snapshot_without_customer(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    before = screenshot(sessions)

    response = commit(test_client, name="  Ahmad  ")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "successful"

    after = screenshot(sessions)
    assert after["transactions"] == before["transactions"] + 1
    assert after["ledger"] == before["ledger"] + 1
    assert after["customers"] == before["customers"]
    assert after["balances"]["mtn"] == before["balances"]["mtn"] - 5000
    assert after["debt"] == before["debt"]

    assert customers(sessions) == []
    assert payload["customer_name"] == "Ahmad"
    rows = stored(sessions)
    assert len(rows) == 1
    assert rows[0]["customer_id"] is None
    assert rows[0]["customer_phone"] == PHONE
    assert rows[0]["status"] == "successful"

    listed = test_client.get("/api/transactions").json()
    assert [(row["id"], row["customer_id"], row["customer_name"]) for row in listed] == [
        (payload["id"], None, "Ahmad")
    ]


def test_named_commit_leaves_existing_customer_and_preferences_unchanged(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    created = test_client.post("/api/customers", json={"phone": PHONE, "name": "Ahmad"}).json()
    assert created["name"] == "Ahmad"

    assert commit(test_client, name="Ahmad").status_code == 200
    # Different sale and spelling: the customer record is untouched.
    assert commit(test_client, name="ahmad", amount_cents=2500).status_code == 200

    assert customers(sessions) == [
        {
            "id": created["id"],
            "phone": PHONE,
            "name": "Ahmad",
            "operator_code": None,
            "service_code": None,
        }
    ]
    rows = stored(sessions)
    assert len(rows) == 2
    assert {row["customer_id"] for row in rows} == {None}
    assert [row["customer_phone"] for row in rows] == [PHONE, PHONE]


def test_name_lookup_keeps_returning_the_first_of_duplicate_names(client) -> None:
    """The established exact-name lookup semantics are unchanged."""
    test_client, _sessions = client
    first = test_client.post("/api/customers", json={"phone": PHONE, "name": "Ahmad"}).json()
    test_client.post("/api/customers", json={"phone": OTHER_PHONE, "name": "Ahmad"})

    response = test_client.get("/api/customers/lookup-by-name", params={"name": "ahmad"})
    assert response.status_code == 200
    assert response.json()["id"] == first["id"]


def test_named_sale_normalizes_its_own_phone_without_linking(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    test_client.post("/api/customers", json={"phone": PHONE, "name": "Ahmad"})

    # A formatted variant of the same number is still that customer's number.
    response = commit(test_client, phone="+963 944-123-456", name="Ahmad")
    assert response.status_code == 200
    rows = stored(sessions)
    assert rows[0]["customer_phone"] == PHONE
    assert rows[0]["customer_id"] is None


def test_ambiguous_customer_names_do_not_block_sale(client) -> None:
    """Customer ambiguity is irrelevant to a sale snapshot."""
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    test_client.post("/api/customers", json={"phone": PHONE, "name": "Ahmad"})
    test_client.post("/api/customers", json={"phone": OTHER_PHONE, "name": "Ahmad"})
    before = screenshot(sessions)

    response = commit(test_client, phone="0955123456", name="Ahmad")
    assert response.status_code == 200
    assert stored(sessions)[0]["customer_id"] is None
    assert screenshot(sessions)["customers"] == before["customers"]
    assert screenshot(sessions)["balances"]["mtn"] == before["balances"]["mtn"] - 5000
    assert len(customers(sessions)) == 2


def test_duplicate_customer_names_never_link_sale(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    first = test_client.post("/api/customers", json={"phone": PHONE, "name": "Ahmad"}).json()
    test_client.post("/api/customers", json={"phone": OTHER_PHONE, "name": "Ahmad"})

    response = commit(test_client, phone=PHONE, name="Ahmad")
    assert response.status_code == 200
    rows = stored(sessions)
    assert rows[0]["customer_id"] is None


def test_conflicting_customer_name_and_phone_do_not_block_sale(client) -> None:
    """A sale snapshot never changes either existing customer."""
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    test_client.post("/api/customers", json={"phone": OTHER_PHONE, "name": "Ahmad"})
    test_client.post("/api/customers", json={"phone": PHONE, "name": "Rami"})
    before = screenshot(sessions)

    response = commit(test_client, phone=PHONE, name="Ahmad")
    assert response.status_code == 200
    assert stored(sessions)[0]["customer_name"] == "Ahmad"
    assert stored(sessions)[0]["customer_id"] is None
    # Neither record was renamed or re-numbered.
    assert customers(sessions) == [
        {
            "id": 1,
            "phone": OTHER_PHONE,
            "name": "Ahmad",
            "operator_code": None,
            "service_code": None,
        },
        {
            "id": 2,
            "phone": PHONE,
            "name": "Rami",
            "operator_code": None,
            "service_code": None,
        },
    ]


def test_registered_name_with_different_number_does_not_block_sale(client) -> None:
    """The typed number is not written over the registered customer's number."""
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    existing = test_client.post("/api/customers", json={"phone": PHONE, "name": "Ahmad"}).json()
    before = screenshot(sessions)

    response = commit(test_client, phone=OTHER_PHONE, name="Ahmad")
    assert response.status_code == 200
    assert stored(sessions)[0]["customer_phone"] == OTHER_PHONE
    assert stored(sessions)[0]["customer_id"] is None
    assert customers(sessions) == [
        {
            "id": existing["id"],
            "phone": PHONE,
            "name": "Ahmad",
            "operator_code": None,
            "service_code": None,
        }
    ]
    assert len(test_client.get("/api/transactions").json()) == 1


def test_a_new_name_for_an_existing_phone_is_not_silently_reused(client) -> None:
    """An existing record for the number is not overwritten by another name."""
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    test_client.post("/api/customers", json={"phone": PHONE, "name": "Rami"})
    before = screenshot(sessions)

    response = commit(test_client, phone=PHONE, name="Ahmad")
    assert response.status_code == 200
    assert stored(sessions)[0]["customer_name"] == "Ahmad"
    assert stored(sessions)[0]["customer_id"] is None
    assert [row["name"] for row in customers(sessions)] == ["Rami"]


# --- 3. A blank name is a one-time sale -----------------------------------


@pytest.mark.parametrize("name", [None, "", "   ", "\t \n"])
def test_blank_name_commit_records_the_sale_without_a_customer(client, name: str) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    before = screenshot(sessions)

    response = commit(test_client, name=name)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "successful"
    assert payload["ussd_string"] == CODE

    after = screenshot(sessions)
    assert after["customers"] == before["customers"] == 0
    assert after["transactions"] == before["transactions"] + 1
    assert after["ledger"] == before["ledger"] + 1
    assert after["balances"]["mtn"] == before["balances"]["mtn"] - 5000
    # No placeholder customer, no debt row.
    assert after["debt"] == 0
    assert customers(sessions) == []

    rows = stored(sessions)
    assert len(rows) == 1
    assert rows[0]["customer_id"] is None
    assert rows[0]["customer_phone"] == PHONE
    assert rows[0]["status"] == "successful"

    # Visible in the API and rendered as a one-time sale, phone included.
    listed = test_client.get("/api/transactions").json()
    assert len(listed) == 1
    assert listed[0]["customer_id"] is None
    assert listed[0]["customer_phone"] == PHONE
    assert listed[0]["customer_name"] is None
    page = test_client.get("/transactions").text
    assert "One-time sale" in page


def test_a_blank_name_does_not_touch_an_unrelated_existing_customer(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    other = test_client.post("/api/customers", json={"phone": OTHER_PHONE, "name": "Rami"}).json()
    before = screenshot(sessions)

    assert commit(test_client, name="  ").status_code == 200

    assert screenshot(sessions)["customers"] == before["customers"]
    assert customers(sessions) == [
        {
            "id": other["id"],
            "phone": OTHER_PHONE,
            "name": "Rami",
            "operator_code": None,
            "service_code": None,
        }
    ]


def test_the_phone_is_never_used_as_a_customer_name(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)

    # A number typed into the name box is just a name; it is not promoted to a
    # customer record for anybody else, and the sale still commits.
    assert commit(test_client, name="Ayah").status_code == 200
    assert commit(test_client, name=None, phone=OTHER_PHONE).status_code == 200

    installed = customers(sessions)
    assert [row["name"] for row in installed] == []
    assert all(row["name"] != row["phone"] for row in installed)


# --- 4. Idempotency and exactly-once accounting ---------------------------


@pytest.mark.parametrize("name", ["Ahmad", None])
def test_replaying_one_commit_key_never_duplicates_anything(client, name: str) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    key = commit_key()

    first = commit(test_client, key=key, name=name)
    assert first.status_code == 200
    snapshot = screenshot(sessions)
    for _ in range(3):
        replay = commit(test_client, key=key, name=name)
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert screenshot(sessions) == snapshot

    assert snapshot["transactions"] == 1
    assert snapshot["ledger"] == 1
    assert snapshot["customers"] == 0
    assert snapshot["balances"]["mtn"] == 95_000


@pytest.mark.parametrize("name", ["Ahmad", None])
def test_concurrent_retries_of_one_key_commit_once(client, name: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    key = "racecustomers00000000001"

    def fire(_index: int):
        return commit(test_client, key=key, name=name)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = [future.result() for future in [pool.submit(fire, i) for i in range(4)]]

    assert all(response.status_code == 200 for response in responses), [
        response.status_code for response in responses
    ]
    assert len({response.json()["reference"] for response in responses}) == 1
    snapshot = screenshot(sessions)
    assert snapshot["transactions"] == 1
    assert snapshot["ledger"] == 1
    assert snapshot["customers"] == 0
    assert snapshot["balances"]["mtn"] == 95_000


def test_distinct_keys_stay_distinct_sales_for_both_kinds(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)

    named = commit(test_client, name="Ahmad")
    anonymous_a = commit(test_client, name=None, phone=OTHER_PHONE)
    anonymous_b = commit(test_client, name="   ", phone="0955123456")
    assert named.status_code == anonymous_a.status_code == anonymous_b.status_code == 200
    assert len({named.json()["id"], anonymous_a.json()["id"], anonymous_b.json()["id"]}) == 3

    snapshot = screenshot(sessions)
    assert snapshot["transactions"] == 3
    assert snapshot["ledger"] == 3
    assert snapshot["customers"] == 0
    assert snapshot["balances"]["mtn"] == 100_000 - 15_000


# --- 5. Failures leave no trace -------------------------------------------


def test_insufficient_balance_creates_neither_transaction_nor_customer(client) -> None:
    test_client, sessions = client
    before = screenshot(sessions)

    named = commit(test_client, name="Ahmad", amount_cents=50_000)
    anonymous = commit(test_client, name=None, amount_cents=50_000)
    assert named.status_code == anonymous.status_code == 400
    assert named.json()["detail"] == "Insufficient SIM balance"

    assert screenshot(sessions) == before
    assert customers(sessions) == []
    assert test_client.get("/api/transactions").json() == []


def test_unknown_configuration_creates_neither_transaction_nor_customer(client) -> None:
    test_client, sessions = client
    fund(test_client)
    before = screenshot(sessions)

    assert commit(test_client, name="Ahmad", operator_code="unknown").status_code == 404
    assert commit(test_client, name=None, service_code="unknown").status_code == 404

    assert screenshot(sessions) == before
    assert customers(sessions) == []


def test_a_colliding_reference_rolls_the_whole_commit_back(client, monkeypatch) -> None:
    from uuid import UUID

    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    monkeypatch.setattr("backend.main.uuid4", lambda: UUID(int=7))

    assert commit(test_client, name="Ahmad").status_code == 200
    snapshot = screenshot(sessions)
    # The frozen uuid4 collides on the reference: nothing of the retry survives.
    assert commit(test_client, name="Rami", phone=OTHER_PHONE).status_code == 409
    assert commit(test_client, name=None, phone="0955123456").status_code == 409

    assert screenshot(sessions) == snapshot
    assert [row["name"] for row in customers(sessions)] == []


# --- 6. Reports and filters keep both kinds of sale -----------------------


def test_list_filters_and_totals_include_one_time_sales(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=500_000)
    fund(test_client, "syriatel", 500_000)
    named = commit(test_client, name="Ahmad").json()
    anonymous = commit(test_client, name=None, phone=OTHER_PHONE, amount_cents=1200).json()
    postpaid = commit(
        test_client,
        name=None,
        phone="0955123456",
        operator_code="syriatel",
        service_code="postpaid",
        amount_cents=0,
    ).json()

    listed = test_client.get("/api/transactions").json()
    assert {row["id"] for row in listed} == {named["id"], anonymous["id"], postpaid["id"]}

    by_operator = test_client.get("/api/transactions", params={"operator_code": "syriatel"}).json()
    assert [row["id"] for row in by_operator] == [postpaid["id"]]

    by_service = test_client.get("/api/transactions", params={"service_code": "prepaid"}).json()
    assert {row["id"] for row in by_service} == {named["id"], anonymous["id"]}

    # The phone filter matches the row's own number, linked or not.
    by_phone = test_client.get("/api/transactions", params={"customer_phone": "0933"}).json()
    assert [row["id"] for row in by_phone] == [anonymous["id"]]
    by_named_phone = test_client.get(
        "/api/transactions", params={"customer_phone": "0944"}
    ).json()
    assert [row["id"] for row in by_named_phone] == [named["id"]]

    by_status = test_client.get("/api/transactions", params={"status": "successful"}).json()
    assert len(by_status) == 3


def test_search_lists_one_time_sales_without_treating_them_as_customers(client) -> None:
    test_client, _sessions = client
    fund(test_client, amount_cents=200_000)
    named = commit(test_client, name="Ahmad").json()
    anonymous = commit(test_client, name=None, phone=OTHER_PHONE).json()

    by_reference = test_client.get("/api/search", params={"q": anonymous["reference"]}).json()
    assert by_reference["customers"] == []
    assert [row["id"] for row in by_reference["transactions"]] == [anonymous["id"]]
    assert by_reference["transactions"][0]["customer_name"] is None

    by_phone = test_client.get("/api/search", params={"q": "0933"}).json()
    assert [row["id"] for row in by_phone["transactions"]] == [anonymous["id"]]
    assert by_phone["customers"] == []

    # Name search matches the transaction snapshot, not a customer record.
    by_name = test_client.get("/api/search", params={"q": "ahmad"}).json()
    assert by_name["customers"] == []
    assert [row["id"] for row in by_name["transactions"]] == [named["id"]]
    by_named_phone = test_client.get("/api/search", params={"q": "0944"}).json()
    assert [row["id"] for row in by_named_phone["transactions"]] == [named["id"]]
    assert by_named_phone["transactions"][0]["customer_name"] == "Ahmad"


def test_operator_page_and_stats_count_one_time_sales(client) -> None:
    test_client, _sessions = client
    fund(test_client, amount_cents=200_000)
    commit(test_client, name="Ahmad", amount_cents=5000)
    commit(test_client, name=None, phone=OTHER_PHONE, amount_cents=1200)

    page = test_client.get("/operators/mtn").text
    assert "Ahmad" in page and OTHER_PHONE in page
    assert "One-time sale" in page
    # Both sales are counted: 5,000 + 1,200 face value.
    assert 'id="operator-tx-count" dir="ltr">2</span>' in page

    stats = test_client.get("/api/stats/summary").json()
    assert stats["totals"]["transaction_count"] == 2
    assert stats["totals"]["volume_cents"] == 6200
    assert stats["by_status"][0] == {
        "status": "successful",
        "count": 2,
        "volume_cents": 6200,
    }


def test_register_summary_counts_one_time_sales(client) -> None:
    test_client, _sessions = client
    fund(test_client, amount_cents=200_000)
    assert test_client.post(
        "/api/register/open", json={"opening_balance_cents": 0}
    ).status_code == 200
    commit(test_client, name=None, phone=OTHER_PHONE, amount_cents=1200)

    summary = test_client.get("/api/register/summary").json()
    assert summary["cash_collected_cents"] == 1200
    assert summary["transaction_count"] == 1


def test_customer_page_queries_stay_customer_only(client) -> None:
    test_client, _sessions = client
    fund(test_client, amount_cents=200_000)
    commit(test_client, name="Ahmad")
    commit(test_client, name=None, phone=OTHER_PHONE)
    commit(test_client, name=None, phone="0955123456")

    listing = test_client.get("/api/customers").json()
    assert [row["name"] for row in listing] == []
    assert [row["phone"] for row in listing] == []
    # No one-time sale leaked into the customer search either.
    matches = test_client.get("/api/customers/search", params={"q": "0933"}).json()
    assert matches == []


# --- 7. Explicit customer registration is unchanged -----------------------


def test_customer_page_creation_still_stores_every_field(client) -> None:
    test_client, sessions = client
    response = test_client.post(
        "/api/customers",
        json={
            "phone": OTHER_PHONE,
            "name": "Lina",
            "operator_code": "syriatel",
            "service_code": "postpaid",
        },
    )
    assert response.status_code == 200
    created = response.json()
    assert set(created) == {"id", "phone", "name", "balance_cents"}
    assert created["phone"] == OTHER_PHONE and created["name"] == "Lina"
    assert customers(sessions) == [
        {
            "id": created["id"],
            "phone": OTHER_PHONE,
            "name": "Lina",
            "operator_code": "syriatel",
            "service_code": "postpaid",
        }
    ]
    # A duplicate phone is still refused, and nothing is created.
    duplicate = test_client.post("/api/customers", json={"phone": OTHER_PHONE, "name": "Other"})
    assert duplicate.status_code == 409
    assert len(customers(sessions)) == 1


def test_an_explicit_customer_needs_no_transaction(client) -> None:
    """Registration on the customer page is independent of a sale."""
    test_client, sessions = client
    before = screenshot(sessions)
    assert test_client.post(
        "/api/customers", json={"phone": PHONE, "name": "Ahmad"}
    ).status_code == 200
    after = screenshot(sessions)
    assert after["customers"] == before["customers"] + 1
    assert after["transactions"] == before["transactions"]
    assert after["ledger"] == before["ledger"]
    assert after["balances"] == before["balances"]


# --- 8. Deleting a customer keeps committed sales -------------------------


def test_deleting_a_customer_keeps_the_sale_snapshot(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    created = test_client.post("/api/customers", json={"phone": PHONE, "name": "Ahmad"}).json()
    sale = commit(test_client, name="Ahmad").json()

    assert test_client.delete(f"/api/customers/{created['id']}").status_code == 204

    rows = stored(sessions)
    assert [(row["id"], row["customer_id"], row["customer_phone"]) for row in rows] == [
        (sale["id"], None, PHONE)
    ]
    listed = test_client.get("/api/transactions").json()
    assert [(row["id"], row["customer_phone"], row["customer_name"]) for row in listed] == [
        (sale["id"], PHONE, "Ahmad")
    ]
    # Its accounting is untouched: no refund, no ledger change.
    assert screenshot(sessions)["ledger"] == 1
    assert screenshot(sessions)["balances"]["mtn"] == 95_000


def test_deleting_a_customer_removes_its_debt_but_keeps_the_profit_entry(client) -> None:
    """Debt belongs to the record; the sale's profit entry belongs to the sale."""
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    created = test_client.post("/api/customers", json={"phone": PHONE, "name": "Ahmad"}).json()
    assert test_client.post(
        "/api/debt/charge",
        json={"customer_phone": PHONE, "amount_cents": 5_000, "description": "Airtime"},
    ).status_code in (200, 201)
    sale = commit(test_client, name="Ahmad").json()
    before = screenshot(sessions)
    assert before["debt"] == 1 and before["ledger"] == 1

    assert test_client.delete(f"/api/customers/{created['id']}").status_code == 204

    after = screenshot(sessions)
    assert after["debt"] == 0
    # The committed sale keeps its own accounting entry and its debit.
    assert after["ledger"] == 1
    assert after["balances"] == before["balances"]
    assert after["transactions"] == 1
    assert test_client.get("/api/transactions").json()[0]["id"] == sale["id"]


def test_imported_rows_without_their_own_phone_are_still_removed_with_the_customer(client) -> None:
    """A legacy row carries no phone of its own; its history lives on the customer."""
    test_client, sessions = client
    created = test_client.post("/api/customers", json={"phone": PHONE, "name": "Ahmad"}).json()

    async def add_legacy_row(session: AsyncSession) -> None:
        session.add(
            Transaction(
                customer_id=created["id"],
                service_id=1,
                amount_cents=4_000,
                status="successful",
                ussd_string="*150*1#",
                reference="TXN-imported",
                idempotency_key="importedkey0000000000001",
                customer_phone=None,
            )
        )
        await session.commit()

    run_db(sessions, add_legacy_row)
    assert screenshot(sessions)["transactions"] == 1

    assert test_client.delete(f"/api/customers/{created['id']}").status_code == 204
    assert stored(sessions) == []


@pytest.mark.parametrize("name", ["  أحمد علي  ", "\tAhmad\n", "", " \t\n", None])
def test_snapshot_is_stored_directly_and_replay_keeps_original_name(client, name) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    key = commit_key()
    expected = (name or "").strip() or None
    first = commit(test_client, key=key, name=name)
    assert first.status_code == 200
    assert first.json()["customer_name"] == expected
    assert stored(sessions)[0]["customer_name"] == expected
    assert stored(sessions)[0]["customer_id"] is None
    assert customers(sessions) == []
    before = screenshot(sessions)
    # Preserve existing key semantics: changing only the name on retry does
    # not overwrite the original sale or trigger another accounting entry.
    retry = commit(test_client, key=key, name="Different name")
    assert retry.json() == first.json()
    assert screenshot(sessions) == before
    listed = test_client.get("/api/transactions").json()
    assert listed[0]["customer_name"] == expected
    searched = test_client.get("/api/search", params={"q": first.json()["reference"]}).json()
    assert searched["transactions"][0]["customer_name"] == expected


def test_customer_edits_and_deletion_never_change_sale_snapshot(client) -> None:
    test_client, sessions = client
    fund(test_client, amount_cents=100_000)
    customer = test_client.post("/api/customers", json={"phone": PHONE, "name": "Ahmad"}).json()
    before_customer = customers(sessions)
    sale = commit(test_client, name="  Ahmad  ").json()
    assert customers(sessions) == before_customer
    assert stored(sessions)[0]["customer_id"] is None
    before_accounting = screenshot(sessions)
    assert test_client.put(f"/api/customers/{customer['id']}", json={"name": "Renamed"}).status_code == 200
    for action in ("edited", "deleted"):
        if action == "deleted":
            assert test_client.delete(f"/api/customers/{customer['id']}").status_code == 204
        assert stored(sessions)[0]["customer_name"] == "Ahmad"
        assert test_client.get("/api/transactions").json()[0]["customer_name"] == "Ahmad"
        matches = test_client.get("/api/search", params={"q": "ahmad"}).json()
        assert matches["customers"] == []
        assert [(row["id"], row["customer_name"]) for row in matches["transactions"]] == [(sale["id"], "Ahmad")]
        after = screenshot(sessions)
        for field in ("transactions", "ledger", "debt", "balances", "legacy"):
            assert after[field] == before_accounting[field]
