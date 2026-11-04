"""Slice 7.5 refinements: homepage redirect, phone prefixes, USSD generation,
the /pos transaction flow, the swapped auto-operator mapping and the mobile
header layout."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, delete, event, select
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
from backend.validation import normalize_phone, validate_syrian_phone


@pytest.fixture
def client(tmp_path) -> Iterator[tuple[TestClient, async_sessionmaker]]:
    """A test client plus the underlying session factory for direct checks."""
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "slice5.db")))
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


def run_db(sessions: async_sessionmaker, statement: Any) -> None:
    """Execute a Core statement against a fresh session."""

    async def execute() -> None:
        async with sessions() as session:
            await session.execute(statement)
            await session.commit()

    asyncio.run(execute())


def test_homepage_redirects_to_pos(client) -> None:
    test_client, _ = client
    response = test_client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/pos"
    followed = test_client.get("/", follow_redirects=True)
    assert followed.status_code == 200
    assert "POS Quick Charge" in followed.text


@pytest.mark.parametrize(
    "phone",
    ["0933123456", "0944123456", "0955123456", "0966123456", "0987123456", "0999123456"],
)
def test_validation_accepts_all_operator_prefixes(client, phone: str) -> None:
    assert validate_syrian_phone(phone) == phone
    assert normalize_phone(phone) == phone
    # Normalized international forms are accepted too.
    assert validate_syrian_phone("+963" + phone[1:]) == phone


@pytest.mark.parametrize(
    "phone",
    ["0922123456", "0901123456", "0977123456", "0911123456", "0923123456", "+964933123456", "12345"],
)
def test_validation_rejects_other_prefixes(client, phone: str) -> None:
    with pytest.raises(ValueError, match="Invalid Syrian mobile number"):
        validate_syrian_phone(phone)


def test_generate_ussd_exact_structures_with_domains_and_fallback(client) -> None:
    test_client, sessions = client
    # Store custom domains through the settings API.
    assert test_client.put("/api/settings/mtn-domain", json={"domain": "11111"}).status_code == 200
    assert test_client.put("/api/settings/syriatel-domain", json={"domain": "22222"}).status_code == 200

    async def check() -> None:
        async with sessions() as session:
            # Exact operator structures with the stored domains.
            assert (
                await generate_ussd(session, "mtn", "prepaid", "0933123456", 5000)
                == "*150*11111*0933123456*500#"
            )
            assert (
                await generate_ussd(session, "mtn", "postpaid", "0933123456", 5000)
                == "*154*11111*0933123456*500#"
            )
            assert (
                await generate_ussd(session, "syriatel", "prepaid", "0944123456", 5000)
                == "*150*1*22222*1*500*0944123456*0944123456#"
            )
            assert (
                await generate_ussd(session, "syriatel", "postpaid", "0944123456", 5000)
                == "*150*1*22222*5*500*0944123456*0944123456#"
            )

            # Without stored settings the default domains apply again.
            await session.execute(
                delete(Setting).where(Setting.key.in_(["MTN_DOMAIN", "SYRIATEL_DOMAIN"]))
            )
            await session.commit()
            assert (
                await generate_ussd(session, "mtn", "prepaid", "0933123456", 5000)
                == "*150*65477*0933123456*500#"
            )
            assert (
                await generate_ussd(session, "mtn", "postpaid", "0933123456", 5000)
                == "*154*65477*0933123456*500#"
            )
            assert (
                await generate_ussd(session, "syriatel", "prepaid", "0944123456", 5000)
                == "*150*1*67858*1*500*0944123456*0944123456#"
            )
            assert (
                await generate_ussd(session, "syriatel", "postpaid", "0944123456", 5000)
                == "*150*1*67858*5*500*0944123456*0944123456#"
            )

    asyncio.run(check())


def test_pos_flow_creates_transaction_deducts_balance_and_records_profit(client) -> None:
    test_client, sessions = client
    assert (
        test_client.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 20000})
        .status_code
        == 200
    )

    response = test_client.post(
        "/api/transactions",
        json={
            "customer_phone": "0933123456",
            "operator_code": "mtn",
            "service_code": "prepaid",
            "amount_cents": 5000,
        },
    )
    assert response.status_code == 200
    transaction = response.json()
    assert transaction["ussd_string"] == "*150*65477*0933123456*500#"
    assert transaction["status"] == "pending"

    # The operator balance was deducted.
    balances = {row["operator_code"]: row for row in test_client.get("/api/operators/balance").json()}
    assert balances["mtn"]["balance_cents"] == 15000

    # The transaction is stored and appears in the transaction list.
    listed = test_client.get("/api/transactions").json()
    assert [row["reference"] for row in listed] == [transaction["reference"]]

    async def check_ledger() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            row = (
                await session.execute(select(Transaction).where(Transaction.id == transaction["id"]))
            ).scalar_one()
            assert row.amount_cents == 5000
            profit = (
                await session.execute(
                    select(ProfitLedger).where(ProfitLedger.transaction_id == row.id)
                )
            ).scalar_one()
            assert profit.operator_code == "mtn"
            assert profit.commission_cents == 250  # 5% of 5000
            assert profit.profit_cents == 4750

    asyncio.run(check_ledger())


def test_pos_template_swapped_prefix_mapping_and_hint(client) -> None:
    test_client, _ = client
    english = test_client.get("/pos")
    arabic = test_client.get("/pos", params={"lang": "ar"})
    assert english.status_code == arabic.status_code == 200
    for rendered in (english.text, arabic.text):
        # Swapped auto-operator mapping: 093/098/099 -> syriatel, 094/095/096 -> mtn.
        assert "'093': 'syriatel'" in rendered
        assert "'098': 'syriatel'" in rendered
        assert "'099': 'syriatel'" in rendered
        assert "'094': 'mtn'" in rendered
        assert "'095': 'mtn'" in rendered
        assert "'096': 'mtn'" in rendered
        assert "'092'" not in rendered
        # Client-side validation accepts 098 as well.
        assert "09[345689]" in rendered
        # The generated code comes from the server, not a client approximation.
        assert "fetch('/api/transactions'" in rendered
        assert "operator.value === 'mtn' ? '100'" not in rendered
    # The accepted-prefix hint mentions 098 in both languages.
    assert "Syrian mobile number starting with 093, 094, 095, 096, 098 or 099." in english.text
    assert "093 أو 094 أو 095 أو 096 أو 098 أو 099" in arabic.text


def test_mobile_header_burger_left_centered_logo(client) -> None:
    test_client, _ = client
    for lang in ("en", "ar"):
        rendered = test_client.get("/pos", params={"lang": lang}).text
        # Mobile row (below sm): three-column grid, burger first, centered logo.
        assert 'grid grid-cols-3 items-center sm:hidden' in rendered
        mobile_row = rendered.split('grid grid-cols-3 items-center sm:hidden')[1].split('</div>')[0]
        assert 'id="nav-toggle"' in mobile_row
        assert 'justify-self-start' in mobile_row
        assert 'justify-self-center text-lg font-bold text-blue-950">Uniter</a>' in mobile_row
        assert mobile_row.index('nav-toggle') < mobile_row.index('justify-self-center')
        # Desktop row (sm and up): logo left, inline nav unchanged.
        assert 'hidden items-center justify-between gap-3 sm:flex' in rendered
        assert rendered.count('id="nav-toggle"') == 1


def test_pages_still_render(client) -> None:
    test_client, _ = client
    for page in ("pos", "settlement", "transactions", "customers", "settings", "topup", "stats", "operators/mtn"):
        for lang in ("en", "ar"):
            response = test_client.get(f"/{page}", params={"lang": lang})
            assert response.status_code == 200, (page, lang)
