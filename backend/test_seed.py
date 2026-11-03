import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import URL, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, Operator, Service, enable_sqlite_foreign_keys
from backend.seed import seed_data


def test_seed_is_idempotent_and_preserves_existing_configuration(tmp_path) -> None:
    async def check() -> None:
        engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(tmp_path / "seed.db"))
        )
        event.listen(engine.sync_engine, "connect", enable_sqlite_foreign_keys)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                await seed_data(session)
                operators = (await session.scalars(select(Operator).order_by(Operator.code))).all()
                assert [(o.code, o.name, o.ussd_prefix, o.is_active) for o in operators] == [
                    ("mtn", "MTN", "100", True),
                    ("syriatel", "Syriatel", "555", True),
                ]
                rows = (await session.execute(
                    select(Operator.code, Service).join(Service).order_by(Operator.code, Service.code)
                )).all()
                assert [(code, service.code, service.name, service.ussd_suffix,
                         service.price_per_unit, service.is_active) for code, service in rows] == [
                    ("mtn", "postpaid", "Post-Paid Bill Payment", "amountphone#", Decimal("0.00"), True),
                    ("mtn", "prepaid", "Pre-Paid Charging", "amountphone#", Decimal("100.00"), True),
                    ("syriatel", "postpaid", "Post-Paid Bill Payment", "amountphone#", Decimal("0.00"), True),
                    ("syriatel", "prepaid", "Pre-Paid Charging", "amountphone#", Decimal("100.00"), True),
                ]
                rows[0][1].ussd_suffix = "custom#"
                await session.commit()
                await seed_data(session)
                await seed_data(session)
                assert len((await session.scalars(select(Operator))).all()) == 2
                services = (await session.scalars(select(Service))).all()
                assert len(services) == 4
                assert rows[0][1].ussd_suffix == "custom#"

                session.add(Service(operator_id=operators[0].id, name="Duplicate",
                                    code="prepaid", ussd_suffix="#", price_per_unit=Decimal("1.00")))
                with pytest.raises(IntegrityError):
                    await session.commit()
                await session.rollback()
                missing = await session.scalar(select(Service).where(Service.code == "prepaid").limit(1))
                await session.delete(missing)
                await session.commit()
                await seed_data(session)
                assert len((await session.scalars(select(Service))).all()) == 4
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_old_unique_constraint_requires_migration(tmp_path) -> None:
    async def check() -> None:
        engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(tmp_path / "old.db"))
        )
        try:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE TABLE services (id INTEGER PRIMARY KEY, code TEXT UNIQUE)"))
                await connection.execute(text("INSERT INTO services (code) VALUES ('prepaid')"))
                # create_all leaves the legacy services table untouched and adds
                # every table the seed now expects on a migrated database.
                await connection.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine)() as session:
                with pytest.raises(RuntimeError, match="Migrate it"):
                    await seed_data(session)
                assert (await session.execute(text("SELECT code FROM services"))).scalars().all() == ["prepaid"]
        finally:
            await engine.dispose()

    asyncio.run(check())
