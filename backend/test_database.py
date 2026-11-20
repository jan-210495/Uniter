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
                "operators", "services", "transactions", "settings",
                "cash_register", "operator_balance",
                "operator_topup", "operator_credit_refund", "profit_ledger", "currency_denomination",
                "sim_cards", "sim_balances", "sim_ledger", "sim_defaults",
                "users", "user_sessions", "login_attempts",
            }
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                operator = database.Operator(name="MTN", code="mtn", ussd_prefix="*123*")
                service = database.Service(
                    operator=operator, name="Prepaid", code="prepaid",
                    ussd_suffix="#", price_per_unit=Decimal("1.25"),
                )
                transaction = database.Transaction(
                    customer_phone="0944123456", customer_name="Ahmad", service=service, amount_cents=125,
                    status="successful", ussd_string="*123*123#", reference="ref-1",
                )
                session.add(transaction)
                await session.commit()
                assert operator.is_active and service.is_active
                assert all((operator.created_at, service.created_at, transaction.created_at))
                assert not hasattr(database, "Customer")
                assert not hasattr(database, "DebtLedger")
                assert "customer_id" not in database.Transaction.__table__.columns
                assert not any(fk.target_fullname.startswith("customers.")
                               for fk in database.Transaction.__table__.foreign_keys)

            async with sessions() as session:
                row = (await session.scalars(
                    select(database.Transaction).options(
                        selectinload(database.Transaction.service).selectinload(database.Service.operator),
                    )
                )).one()
                assert row.customer_name == "Ahmad"
                assert row.customer_phone == "0944123456"
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
                session.add(database.Transaction(customer_phone="0944123456", customer_name="Ahmad", service=service,
                    amount_cents=100, status="invalid", ussd_string="*1#", reference="ref"))
                with pytest.raises(IntegrityError):
                    await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(check())
