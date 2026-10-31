import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import URL, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from backend import database


def test_models_and_relationships(tmp_path, monkeypatch) -> None:
    async def check() -> None:
        engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(tmp_path / "test.db"))
        )
        event.listen(engine.sync_engine, "connect", database.enable_sqlite_foreign_keys)
        monkeypatch.setattr(database, "engine", engine)
        try:
            await database.init_db()
            assert set(database.Base.metadata.tables) == {
                "operators", "services", "customers", "transactions"
            }
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                operator = database.Operator(name="MTN", code="mtn", ussd_prefix="*123*")
                service = database.Service(
                    operator=operator, name="Prepaid", code="prepaid",
                    ussd_suffix="#", price_per_unit=Decimal("1.25"),
                )
                customer = database.Customer(phone="123", name="Customer")
                transaction = database.Transaction(
                    customer=customer, service=service, amount_cents=125,
                    status="pending", ussd_string="*123*123#", reference="ref-1",
                )
                session.add(transaction)
                await session.commit()
                assert operator.is_active and service.is_active
                assert customer.balance_cents == 0
                assert all((operator.created_at, service.created_at, customer.created_at,
                            customer.updated_at, transaction.created_at))
                previous_updated_at = customer.updated_at
                customer.name = "Updated"
                await session.commit()
                assert customer.updated_at > previous_updated_at

            async with sessions() as session:
                row = (await session.scalars(
                    select(database.Transaction).options(
                        selectinload(database.Transaction.customer),
                        selectinload(database.Transaction.service).selectinload(database.Service.operator),
                    )
                )).one()
                assert row.customer.name == "Updated"
                assert row.service.operator.code == "mtn"
                assert row.service.price_per_unit == Decimal("1.25")
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_constraints_and_foreign_keys(tmp_path, monkeypatch) -> None:
    async def check() -> None:
        engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(tmp_path / "test.db"))
        )
        assert event.contains(database.engine.sync_engine, "connect", database.enable_sqlite_foreign_keys)
        event.listen(engine.sync_engine, "connect", database.enable_sqlite_foreign_keys)
        monkeypatch.setattr(database, "engine", engine)
        try:
            await database.init_db()
            sessions = async_sessionmaker(engine)
            async with sessions() as session:
                session.add_all([
                    database.Operator(name="MTN", code="mtn", ussd_prefix="*1*"),
                    database.Operator(name="MTN", code="other", ussd_prefix="*2*"),
                ])
                with pytest.raises(IntegrityError):
                    await session.commit()
                await session.rollback()
                session.add(database.Service(
                    operator_id=999, name="Invalid", code="invalid",
                    ussd_suffix="#", price_per_unit=Decimal("1.00"),
                ))
                with pytest.raises(IntegrityError):
                    await session.commit()
                await session.rollback()
                operator = database.Operator(name="MTN", code="mtn", ussd_prefix="*1*")
                service = database.Service(operator=operator, name="Prepaid", code="prepaid",
                                           ussd_suffix="#", price_per_unit=Decimal("1.00"))
                customer = database.Customer(phone="123", name="Customer")
                session.add(database.Transaction(customer=customer, service=service,
                    amount_cents=100, status="invalid", ussd_string="*1#", reference="ref"))
                with pytest.raises(IntegrityError):
                    await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(check())
