"""Tests for the stats dashboard: GET /api/stats/summary and the /stats page."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import (
    Base,
    Customer,
    Operator,
    ProfitLedger,
    Service,
    Transaction,
    enable_sqlite_foreign_keys,
    get_db,
)
from backend.main import app
from backend.seed import seed_data


@pytest.fixture
def stats_env(tmp_path) -> Iterator[tuple[TestClient, async_sessionmaker]]:
    """A test client plus the underlying session factory for direct inserts."""
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "stats.db")))
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


def insert_transaction(
    sessions: async_sessionmaker,
    *,
    phone: str,
    name: str,
    operator_code: str,
    service_code: str,
    amount_cents: int,
    status: str,
    created_at: datetime,
    commission_cents: int | None = None,
) -> None:
    """Insert one transaction (and optional profit ledger row) at a set time."""

    async def insert() -> None:
        async with sessions() as session:
            customer = await session.scalar(select(Customer).where(Customer.phone == phone))
            if customer is None:
                customer = Customer(phone=phone, name=name)
                session.add(customer)
                await session.flush()
            operator = await session.scalar(select(Operator).where(Operator.code == operator_code))
            assert operator is not None
            service = await session.scalar(
                select(Service).where(Service.operator_id == operator.id, Service.code == service_code)
            )
            assert service is not None
            transaction = Transaction(
                customer_id=customer.id,
                service_id=service.id,
                amount_cents=amount_cents,
                status=status,
                ussd_string="*150*65477*000000000*0#",
                reference=f"TXN-{uuid4().hex}",
                created_at=created_at,
            )
            session.add(transaction)
            await session.flush()
            if commission_cents is not None:
                session.add(
                    ProfitLedger(
                        transaction_id=transaction.id,
                        operator_code=operator_code,
                        amount_cents=amount_cents,
                        commission_cents=commission_cents,
                        profit_cents=amount_cents - commission_cents,
                        created_at=created_at,
                    )
                )
            await session.commit()

    asyncio.run(insert())


def seed_window_data(sessions: async_sessionmaker) -> datetime:
    """Insert hand-computable rows: 4 inside the 7-day window, 1 far outside.

    Returns the "now" the expectations below are computed against.
    """
    now = datetime.now(timezone.utc)
    # MTN: profit 9,500 (comm 500); successful; yesterday.
    insert_transaction(
        sessions,
        phone="0933123456",
        name="Ahmad",
        operator_code="mtn",
        service_code="prepaid",
        amount_cents=10_000,
        status="successful",
        created_at=now - timedelta(days=1),
        commission_cents=500,
    )
    # MTN: profit 2,375 (comm 125); successful; three days ago.
    insert_transaction(
        sessions,
        phone="0933123456",
        name="Ahmad",
        operator_code="mtn",
        service_code="prepaid",
        amount_cents=2_500,
        status="successful",
        created_at=now - timedelta(days=3),
        commission_cents=125,
    )
    # Syriatel: profit 3,800 (comm 200); successful; two days ago.
    insert_transaction(
        sessions,
        phone="0944123456",
        name="Rami",
        operator_code="syriatel",
        service_code="prepaid",
        amount_cents=4_000,
        status="successful",
        created_at=now - timedelta(days=2),
        commission_cents=200,
    )
    # MTN: failed transaction without a profit ledger row; yesterday.
    insert_transaction(
        sessions,
        phone="0933123456",
        name="Ahmad",
        operator_code="mtn",
        service_code="prepaid",
        amount_cents=1_000,
        status="failed",
        created_at=now - timedelta(days=1),
    )
    # Far outside every window used in these tests.
    insert_transaction(
        sessions,
        phone="0955123456",
        name="Amira",
        operator_code="mtn",
        service_code="prepaid",
        amount_cents=100_000,
        status="successful",
        created_at=now - timedelta(days=30),
        commission_cents=5_000,
    )
    return now


def test_summary_shape_and_defaults(stats_env) -> None:
    client, _ = stats_env
    response = client.get("/api/stats/summary")
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"period_days", "operator_code", "totals", "by_operator", "by_status", "daily"}
    assert data["period_days"] == 7
    assert data["operator_code"] is None
    assert set(data["totals"]) == {"profit_cents", "commission_cents", "volume_cents", "transaction_count"}
    assert data["totals"] == {
        "profit_cents": 0,
        "commission_cents": 0,
        "volume_cents": 0,
        "transaction_count": 0,
    }
    # Both seeded operators appear, zeroed, ordered by code on the profit tie.
    assert [
        {
            "operator_code": row["operator_code"],
            "operator_name": row["operator_name"],
            "profit_cents": row["profit_cents"],
            "commission_cents": row["commission_cents"],
            "volume_cents": row["volume_cents"],
            "transaction_count": row["transaction_count"],
        }
        for row in data["by_operator"]
    ] == [
        {
            "operator_code": "mtn",
            "operator_name": "MTN",
            "profit_cents": 0,
            "commission_cents": 0,
            "volume_cents": 0,
            "transaction_count": 0,
        },
        {
            "operator_code": "syriatel",
            "operator_name": "Syriatel",
            "profit_cents": 0,
            "commission_cents": 0,
            "volume_cents": 0,
            "transaction_count": 0,
        },
    ]
    # Both persisted statuses are present even with zero counts.
    assert data["by_status"] == [
        {"status": "successful", "count": 0, "volume_cents": 0},
        {"status": "failed", "count": 0, "volume_cents": 0},
    ]
    # Daily series: 7 zero-filled days, oldest first, ending today.
    today = datetime.now(timezone.utc).date().isoformat()
    assert len(data["daily"]) == 7
    assert [row["date"] for row in data["daily"]] == [
        (datetime.now(timezone.utc).date() - timedelta(days=offset)).isoformat()
        for offset in range(6, -1, -1)
    ]
    assert data["daily"][-1]["date"] == today
    assert all(
        row == {"date": row["date"], "profit_cents": 0, "volume_cents": 0, "transaction_count": 0}
        for row in data["daily"]
    )


def test_period_validation(stats_env) -> None:
    client, _ = stats_env
    today = datetime.now(timezone.utc).date()
    one = client.get("/api/stats/summary", params={"period": 1})
    assert one.status_code == 200
    assert one.json()["period_days"] == 1
    assert [row["date"] for row in one.json()["daily"]] == [today.isoformat()]

    ninety = client.get("/api/stats/summary", params={"period": 90})
    assert ninety.status_code == 200
    assert ninety.json()["period_days"] == 90
    assert len(ninety.json()["daily"]) == 90
    assert ninety.json()["daily"][0]["date"] == (today - timedelta(days=89)).isoformat()
    assert ninety.json()["daily"][-1]["date"] == today.isoformat()

    for invalid in ("91", "0", "-1", "abc"):
        response = client.get("/api/stats/summary", params={"period": invalid})
        assert response.status_code == 422, invalid


def test_totals_breakdowns_and_daily_match_hand_computation(stats_env) -> None:
    client, sessions = stats_env
    now = seed_window_data(sessions)

    data = client.get("/api/stats/summary").json()
    assert data["period_days"] == 7
    assert data["totals"] == {
        "profit_cents": 15_675,  # 9,500 + 2,375 + 3,800
        "commission_cents": 825,  # 500 + 125 + 200
        "volume_cents": 17_500,  # 10,000 + 2,500 + 4,000 + 1,000 (failed counts, no profit)
        "transaction_count": 4,
    }
    by_operator = {row["operator_code"]: row for row in data["by_operator"]}
    assert by_operator["mtn"] == {
        "operator_code": "mtn",
        "operator_name": "MTN",
        "profit_cents": 11_875,  # 9,500 + 2,375
        "commission_cents": 625,
        "volume_cents": 13_500,  # 10,000 + 2,500 + 1,000
        "transaction_count": 3,
    }
    assert by_operator["syriatel"] == {
        "operator_code": "syriatel",
        "operator_name": "Syriatel",
        "profit_cents": 3_800,
        "commission_cents": 200,
        "volume_cents": 4_000,
        "transaction_count": 1,
    }
    # Sorted by profit descending (MTN first).
    assert [row["operator_code"] for row in data["by_operator"]] == ["mtn", "syriatel"]
    assert data["by_status"] == [
        {"status": "successful", "count": 3, "volume_cents": 16_500},
        {"status": "failed", "count": 1, "volume_cents": 1_000},
    ]

    daily = {row["date"]: row for row in data["daily"]}
    assert len(daily) == 7  # zero-filled, one row per calendar day
    assert daily[(now - timedelta(days=1)).date().isoformat()] == {
        "date": (now - timedelta(days=1)).date().isoformat(),
        "profit_cents": 9_500,
        "volume_cents": 11_000,  # 10,000 successful + 1,000 failed
        "transaction_count": 2,
    }
    assert daily[(now - timedelta(days=2)).date().isoformat()]["profit_cents"] == 3_800
    assert daily[(now - timedelta(days=2)).date().isoformat()]["volume_cents"] == 4_000
    assert daily[(now - timedelta(days=3)).date().isoformat()]["profit_cents"] == 2_375
    assert daily[(now - timedelta(days=3)).date().isoformat()]["transaction_count"] == 1
    # Days without activity are zero, the 30-day-old row is excluded, and the
    # series is ordered oldest to newest.
    dates = [row["date"] for row in data["daily"]]
    assert dates == sorted(dates)
    empty_days = [row for row in data["daily"] if row["transaction_count"] == 0]
    assert len(empty_days) == 4
    assert all(row["profit_cents"] == 0 and row["volume_cents"] == 0 for row in empty_days)
    assert all(row["date"] != (now - timedelta(days=30)).date().isoformat() for row in data["daily"])


def test_operator_filter_restricts_every_section(stats_env) -> None:
    client, sessions = stats_env
    now = seed_window_data(sessions)

    data = client.get("/api/stats/summary", params={"operator_code": "mtn"}).json()
    assert data["operator_code"] == "mtn"
    assert data["totals"] == {
        "profit_cents": 11_875,
        "commission_cents": 625,
        "volume_cents": 13_500,
        "transaction_count": 3,
    }
    assert [row["operator_code"] for row in data["by_operator"]] == ["mtn"]
    # MTN keeps two successful sales and the failed one.
    assert data["by_status"] == [
        {"status": "successful", "count": 2, "volume_cents": 12_500},
        {"status": "failed", "count": 1, "volume_cents": 1_000},
    ]
    daily = {row["date"]: row for row in data["daily"]}
    assert daily[(now - timedelta(days=2)).date().isoformat()]["volume_cents"] == 0
    assert daily[(now - timedelta(days=2)).date().isoformat()]["transaction_count"] == 0
    assert daily[(now - timedelta(days=1)).date().isoformat()]["transaction_count"] == 2


def test_unknown_operator_filter_is_permissive(stats_env) -> None:
    client, sessions = stats_env
    seed_window_data(sessions)

    response = client.get("/api/stats/summary", params={"operator_code": "unknown"})
    assert response.status_code == 200
    data = response.json()
    assert data["operator_code"] == "unknown"
    assert data["by_operator"] == []
    assert data["totals"] == {
        "profit_cents": 0,
        "commission_cents": 0,
        "volume_cents": 0,
        "transaction_count": 0,
    }
    assert data["by_status"] == [
        {"status": "successful", "count": 0, "volume_cents": 0},
        {"status": "failed", "count": 0, "volume_cents": 0},
    ]
    assert len(data["daily"]) == 7
    assert all(row["transaction_count"] == 0 for row in data["daily"])


def test_stats_page_english_and_arabic(stats_env) -> None:
    client, _ = stats_env
    english = client.get("/stats")
    assert english.status_code == 200
    assert "<title>Stats · Uniter</title>" in english.text
    assert 'dir="ltr"' in english.text
    # Nav link is highlighted only on the stats page itself.
    assert 'aria-current="page">Stats</a>' in english.text
    assert ">Statistics</h1>" in english.text

    arabic = client.get("/stats", params={"lang": "ar"})
    assert arabic.status_code == 200
    assert "<title>الإحصائيات · Uniter</title>" in arabic.text
    assert 'dir="rtl"' in arabic.text
    assert ">الإحصائيات</h1>" in arabic.text
    assert 'href="/stats"' in arabic.text


def test_stats_nav_link_on_other_pages(stats_env) -> None:
    client, _ = stats_env
    for page in ("pos", "settings", "customers"):
        for lang in ("en", "ar"):
            response = client.get(f"/{page}", params={"lang": lang})
            assert response.status_code == 200, (page, lang)
            # Present once in the shared desktop/mobile side list, unhighlighted.
            assert response.text.count('href="/stats"') == 1, (page, lang)
            assert 'aria-current="page">Stats<' not in response.text or page != "stats"


def test_stats_page_markup(stats_env) -> None:
    client, _ = stats_env
    response = client.get("/stats")
    assert response.status_code == 200
    # Stat cards, per-operator table, status badges, daily table, live region.
    for element_id in (
        "stats-profit",
        "stats-commission",
        "stats-volume",
        "stats-count",
        "stats-operator-rows",
        "stats-successful-count",
        "stats-failed-count",
        "stats-daily-rows",
        "stats-status",
        "stats-empty",
    ):
        assert f'id="{element_id}"' in response.text, element_id
    assert 'role="status"' in response.text
    assert 'aria-labelledby="stats-operator-title"' in response.text
    assert 'aria-labelledby="stats-daily-title"' in response.text
    # Single client-side fetch of the summary endpoint with the default period.
    assert "fetch('/api/stats/summary?period=7')" in response.text
    assert "toLocaleString('en-US')" in response.text
    assert "No data yet." in response.text
    assert ">SYP<" in response.text or "SYP</span>" in response.text
    arabic = client.get("/stats", params={"lang": "ar"})
    assert "لا توجد بيانات بعد." in arabic.text
    assert "ل.س" in arabic.text
