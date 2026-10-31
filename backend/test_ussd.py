import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import URL, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, Operator, Service, enable_sqlite_foreign_keys
from backend.seed import generate_ussd, seed_data


def test_generate_ussd(tmp_path) -> None:
    async def check() -> None:
        engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(tmp_path / "ussd.db"))
        )
        event.listen(engine.sync_engine, "connect", enable_sqlite_foreign_keys)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                await seed_data(session)
                assert await generate_ussd(session, "mtn", "prepaid", "0933123456", 5000) == "10050*0933123456#"
                assert await generate_ussd(session, "syriatel", "postpaid", "0944123456", 0) == "5550*0944123456#"
                assert await generate_ussd(session, "mtn", "prepaid", "0933123456", 5050) == "10050.5*0933123456#"
                assert await generate_ussd(session, "mtn", "prepaid", "0933123456", 1) == "1000.01*0933123456#"

                service = await session.scalar(
                    select(Service).join(Operator).where(Operator.code == "mtn", Service.code == "prepaid")
                )
                service.ussd_suffix = "*amount*phone#"
                await session.flush()
                assert await generate_ussd(session, "mtn", "prepaid", "0933123456", 5000) == "100*50*0933123456#"
                assert await generate_ussd(session, "syriatel", "prepaid", "0944123456", 5000) == "55550*0944123456#"

                session.add(Service(operator_id=service.operator_id, name="MTN Only", code="mtn-only",
                                    ussd_suffix="amountphone#", price_per_unit=Decimal("0.00")))
                await session.flush()
                with pytest.raises(ValueError, match="Operator not found: unknown"):
                    await generate_ussd(session, "unknown", "prepaid", "0933123456", 5000)
                with pytest.raises(ValueError, match="Service not found for operator syriatel: mtn-only"):
                    await generate_ussd(session, "syriatel", "mtn-only", "0944123456", 0)
        finally:
            await engine.dispose()

    asyncio.run(check())
