import asyncio

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
                # Operator structures with the default domain settings.
                assert await generate_ussd(session, "mtn", "prepaid", "0933123456", 5000) == "*150*65477*0933123456*500#"
                assert await generate_ussd(session, "mtn", "postpaid", "0933123456", 5000) == "*154*65477*0933123456*500#"
                assert await generate_ussd(session, "syriatel", "prepaid", "0944123456", 5000) == "*150*1*67858*1*500*0944123456*0944123456#"
                assert await generate_ussd(session, "syriatel", "postpaid", "0944123456", 5000) == "*150*1*67858*5*500*0944123456*0944123456#"
                # Amounts render in tens of cents, keeping one decimal when needed.
                assert await generate_ussd(session, "mtn", "prepaid", "0933123456", 0) == "*150*65477*0933123456*0#"
                assert await generate_ussd(session, "mtn", "prepaid", "0933123456", 5050) == "*150*65477*0933123456*505#"
                assert await generate_ussd(session, "mtn", "prepaid", "0933123456", 1) == "*150*65477*0933123456*0.1#"

                # The service must belong to the operator.
                with pytest.raises(ValueError, match="Operator not found: unknown"):
                    await generate_ussd(session, "unknown", "prepaid", "0933123456", 5000)
                with pytest.raises(ValueError, match="Service not found for operator syriatel: prepaid-only"):
                    await generate_ussd(session, "syriatel", "prepaid-only", "0944123456", 0)

                # Service codes without a USSD mode are rejected.
                service = await session.scalar(
                    select(Service).join(Operator).where(Operator.code == "mtn", Service.code == "prepaid")
                )
                service.code = "mtn-only"
                await session.flush()
                with pytest.raises(ValueError, match="Unsupported service code for USSD: mtn-only"):
                    await generate_ussd(session, "mtn", "mtn-only", "0933123456", 5000)
        finally:
            await engine.dispose()

    asyncio.run(check())
