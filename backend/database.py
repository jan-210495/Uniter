from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import URL, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, relationship

DATABASE_URL = URL.create(
    "sqlite+aiosqlite", database=os.getenv("UNITER_DB", "uniter.db")
)
engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()


@event.listens_for(engine.sync_engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Operator(Base):
    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    ussd_prefix: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    services: Mapped[list[Service]] = relationship(back_populates="operator")


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("operator_id", "code", name="uq_service_operator_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id"))
    name: Mapped[str] = mapped_column(String(255))
    code: Mapped[str] = mapped_column(String(50))
    ussd_suffix: Mapped[str] = mapped_column(String(255))
    price_per_unit: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    operator: Mapped[Operator] = relationship(back_populates="services")
    transactions: Mapped[list[Transaction]] = relationship(back_populates="service")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    transactions: Mapped[list[Transaction]] = relationship(back_populates="customer")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'completed', 'failed')", name="transaction_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    ussd_string: Mapped[str] = mapped_column(String(255))
    reference: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    customer: Mapped[Customer] = relationship(back_populates="transactions")
    service: Mapped[Service] = relationship(back_populates="transactions")


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
