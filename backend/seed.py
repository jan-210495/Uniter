"""Seed the initial operator and service configuration.

Databases created before service codes became unique per operator must be
migrated before running this seed; create_all does not alter existing tables.
"""

import asyncio
from decimal import Decimal

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

if __package__:
    from .database import Operator, Service, SessionLocal, init_db
else:
    from database import Operator, Service, SessionLocal, init_db


async def seed_data(session: AsyncSession) -> None:
    """Insert missing configuration and commit; leave existing rows unchanged."""
    connection = await session.connection()
    legacy_unique_code = await connection.run_sync(
        lambda conn: any(
            constraint["column_names"] == ["code"]
            for constraint in inspect(conn).get_unique_constraints("services")
        )
    )
    if legacy_unique_code:
        raise RuntimeError(
            "The services table has the old globally unique code constraint. "
            "Migrate it to a unique (operator_id, code) constraint before seeding."
        )

    operators = (
        ("MTN", "mtn", "100"),
        ("Syriatel", "syriatel", "555"),
    )
    services = (
        ("Pre-Paid Charging", "prepaid", Decimal("100.00")),
        ("Post-Paid Bill Payment", "postpaid", Decimal("0.00")),
    )

    for name, code, prefix in operators:
        operator = await session.scalar(select(Operator).where(Operator.code == code))
        if operator is None:
            operator = Operator(name=name, code=code, ussd_prefix=prefix, is_active=True)
            session.add(operator)
            await session.flush()

        for service_name, service_code, price in services:
            service = await session.scalar(
                select(Service).where(
                    Service.operator_id == operator.id, Service.code == service_code
                )
            )
            if service is None:
                session.add(
                    Service(
                        operator_id=operator.id,
                        name=service_name,
                        code=service_code,
                        ussd_suffix="amountphone#",
                        price_per_unit=price,
                        is_active=True,
                    )
                )

    await session.commit()


async def main() -> None:
    await init_db()
    async with SessionLocal() as session:
        await seed_data(session)


if __name__ == "__main__":
    asyncio.run(main())
