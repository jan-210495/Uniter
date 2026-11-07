from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import URL, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, event, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    # Optional POS auto-recognition preferences (free-form codes, not FKs).
    operator_code: Mapped[str | None] = mapped_column(String(50), nullable=True, default=None)
    service_code: Mapped[str | None] = mapped_column(String(50), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    transactions: Mapped[list[Transaction]] = relationship(back_populates="customer")
    debt_ledger: Mapped[list[DebtLedger]] = relationship(back_populates="customer")


#: The only statuses a persisted transaction can carry. A row exists only once
#: the seller committed the sale by copying the code or opening the dialer.
TRANSACTION_STATUSES = ("successful", "failed")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("status IN ('successful', 'failed')", name="transaction_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    ussd_string: Mapped[str] = mapped_column(String(255))
    reference: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # Client-supplied commit key: the same sale commits exactly once, no matter
    # how often the tap or the request is replayed.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, default=None
    )
    # Tombstone for removed failed rows: the row and its ledger stay on disk.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Migration provenance for rows imported from the pending/completed schema.
    legacy_status: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)

    customer: Mapped[Customer] = relationship(back_populates="transactions")
    service: Mapped[Service] = relationship(back_populates="transactions")


class DebtLedger(Base):
    __tablename__ = "debt_ledger"
    __table_args__ = (
        CheckConstraint("type IN ('charge', 'payment')", name="debt_ledger_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    register_id: Mapped[int | None] = mapped_column(ForeignKey("cash_register.id"), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(512), default="")
    type: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    customer: Mapped[Customer] = relationship(back_populates="debt_ledger")


class OperatorBalance(Base):
    """Prepaid credit the shop holds with an operator (one row per operator)."""

    __tablename__ = "operator_balance"

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id"), unique=True)
    balance_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class OperatorTopup(Base):
    """Audit trail of credit top-ups made against an operator balance."""

    __tablename__ = "operator_topup"

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id"))
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reference: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProfitLedger(Base):
    """Commission profit per transaction, tracked separately from operator balances."""

    __tablename__ = "profit_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), unique=True)
    operator_code: Mapped[str] = mapped_column(String(50))
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    commission_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    profit_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CashRegister(Base):
    __tablename__ = "cash_register"
    __table_args__ = (
        # Only one register can be open at a time - enforced in code
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    opening_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    closing_balance_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class CurrencyDenomination(Base):
    __tablename__ = "currency_denomination"
    __table_args__ = (
        # Only one denomination can be active at a time - enforced in code
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scale_name: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    multiplier: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(255), unique=True)
    value: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


async def get_setting(
    session: AsyncSession, key: str, default: str | None = None
) -> str | None:
    value = await session.scalar(select(Setting.value).where(Setting.key == key))
    return default if value is None else value


async def set_setting(session: AsyncSession, key: str, value: str) -> Setting:
    """Upsert a setting in the caller's transaction; the caller commits it."""
    statement = sqlite_insert(Setting).values(key=key, value=value, updated_at=utc_now())
    statement = statement.on_conflict_do_update(
        index_elements=[Setting.key],
        set_={"value": value, "updated_at": utc_now()},
    )
    await session.execute(statement)
    result = await session.scalar(
        select(Setting).where(Setting.key == key).execution_options(populate_existing=True)
    )
    assert result is not None
    return result


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def _migrate_schema() -> None:
    """Apply the guarded schema upgrades to an existing database."""
    try:
        from .migrations import migrate_transaction_lifecycle
    except ImportError:  # pragma: no cover - flat module layout (PyInstaller)
        from migrations import migrate_transaction_lifecycle

    await migrate_transaction_lifecycle(engine)


async def upgrade_existing_database() -> bool:
    """Upgrade an already-existing database file before the first request.

    ``create_all`` never alters an existing table, so a database written by an
    earlier version must be rebuilt before the app can write to it. A missing
    file is left alone: fresh databases are created (and migrated) by the
    seeding entry points, and nothing is ever created implicitly here.
    """
    path = DATABASE_URL.database
    if not path or not os.path.exists(path):
        return False
    await _migrate_schema()
    return True


async def init_db() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        statement = sqlite_insert(Setting).values(
            key="commission_rate", value="0.05", updated_at=utc_now()
        ).on_conflict_do_nothing(index_elements=[Setting.key])
        await connection.execute(statement)
    await _migrate_schema()
