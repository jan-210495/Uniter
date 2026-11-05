import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import database
from backend.commission import calculate_commission, get_commission_for_transaction


@pytest.mark.parametrize(
    ("amount", "rate", "commission", "net"),
    [(5000, "0.05", 250, 4750), (10, "0.05", 0, 10), (30, "0.05", 2, 28),
     (999, "0", 0, 999), (999, "1", 999, 0),
     (10, "0.05000000000000000000000000001", 1, 9)],
)
def test_calculate_commission(amount: int, rate: str, commission: int, net: int) -> None:
    assert calculate_commission(amount, Decimal(rate)) == {
        "amount_cents": amount, "commission_cents": commission,
        "net_cents": net, "total_cents": amount + commission, "rate": rate,
    }


@pytest.mark.parametrize("rate", ["-0.01", "1.01", "NaN", "Infinity", "-Infinity"])
def test_reject_invalid_rates(rate: str) -> None:
    with pytest.raises(ValueError, match="rate"):
        calculate_commission(5000, Decimal(rate))


def test_reject_negative_amount() -> None:
    with pytest.raises(ValueError, match="Amount"):
        calculate_commission(-1, Decimal("0.05"))


def test_settings_seed_updates_and_missing_rate(tmp_path, monkeypatch) -> None:
    async def check() -> None:
        engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "settings.db")))
        event.listen(engine.sync_engine, "connect", database.enable_sqlite_foreign_keys)
        monkeypatch.setattr(database, "engine", engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(database.Base.metadata.create_all)
            async with sessions() as session:
                assert await database.get_setting(session, "missing") is None
                assert await database.get_setting(session, "missing", "fallback") == "fallback"
                assert (await get_commission_for_transaction(session, 5000))["commission_cents"] == 250
            await database.init_db()
            async with sessions() as session:
                assert await database.get_setting(session, "commission_rate") == "0.05"
                first = await database.set_setting(session, "commission_rate", "0.12")
                first_id = first.id
                await session.commit()
                assert (await get_commission_for_transaction(session, 5000))["commission_cents"] == 600
            await database.init_db()
            async with sessions() as session:
                assert await database.get_setting(session, "commission_rate") == "0.12"
                setting = await database.set_setting(session, "commission_rate", "0.30")
                assert setting.id == first_id
                await session.rollback()
            async with sessions() as session:
                assert await database.get_setting(session, "commission_rate") == "0.12"
                await database.set_setting(session, "commission_rate", "bad")
                await session.commit()
                with pytest.raises(ValueError, match="valid decimal"):
                    await get_commission_for_transaction(session, 5000)
        finally:
            await engine.dispose()

    asyncio.run(check())
