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
    # A sale to a one-time (phone-only) buyer has no customer record: the link
    # stays NULL and the phone is kept on the row itself, see ``customer_phone``.
    # Customer records exist for repeat, named customers only.
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id"), nullable=True, default=None
    )
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
    # Provenance for rows imported by the lifecycle upgrade: the status this row
    # had in the pending/completed schema, kept so the mapping stays traceable
    # and reversible.
    legacy_status: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    # The number the seller charged. Stored on the row for every sale so a
    # one-time (unlinked) sale is still identifiable in history, filters and
    # search, and so history always shows the number that was actually charged.
    customer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)

    customer: Mapped[Customer | None] = relationship(back_populates="transactions")
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


class OperatorCreditRefund(Base):
    """Append-only face-credit reversal; original profit/debit history is untouched.

    The reference survives legacy customer-history deletion; the optional FK is
    cleared by SQLite on deletion, preserving existing customer deletion policy.
    """

    __tablename__ = "operator_credit_refund"
    __table_args__ = (CheckConstraint("amount_cents >= 0", name="refund_amount"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    transaction_reference: Mapped[str] = mapped_column(String(255), unique=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id"))
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
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
    await migrate_operator_credit_refunds(engine)


async def upgrade_existing_database() -> bool:
    """Upgrade an already-existing database file before the first request.

    ``create_all`` never alters an existing table, so a database written by an
    earlier version must be rebuilt before the app can write to it. The policy
    lives with the migration itself:

    * missing file — untouched: fresh databases are created (and already
      current) by the seeding entry points; nothing is created implicitly here;
    * empty file — reported as uninitialised, nothing to migrate;
    * partially initialised schema — :class:`SchemaUpgradeError` so the app
      fails closed instead of serving a schema it cannot write to;
    * existing v1.0.x database — atomic, idempotent rebuild.

    Any failure propagates: the caller (startup hook, ``init_db`` or the desktop
    seed) stops instead of serving an incompatible schema, and the database file
    is left as it was found.
    """
    try:
        from .migrations import upgrade_existing_database as _upgrade
    except ImportError:  # pragma: no cover - flat module layout (PyInstaller)
        from migrations import upgrade_existing_database as _upgrade

    if DATABASE_URL.database and os.path.exists(DATABASE_URL.database):
        await validate_existing_refund_schema(engine)
    changed = await _upgrade(engine, DATABASE_URL.database)
    if DATABASE_URL.database and os.path.exists(DATABASE_URL.database):
        changed = await migrate_operator_credit_refunds(engine) or changed
    return changed


def _validate_refund_schema(connection) -> None:
    """Fail closed on a preexisting audit that cannot enforce our invariants."""
    from sqlalchemy import create_engine, inspect
    try:
        from .migrations import SchemaUpgradeError
    except ImportError:  # pragma: no cover - frozen flat layout
        from migrations import SchemaUpgradeError

    def signature(inspector):
        table = OperatorCreditRefund.__tablename__
        columns = tuple(
            (c['name'], str(c['type']), c['nullable'], c['default'])
            for c in inspector.get_columns(table)
        )
        primary = tuple(inspector.get_pk_constraint(table)['constrained_columns'])
        foreign = sorted(
            (tuple(f['constrained_columns']), f['referred_table'],
             tuple(f['referred_columns']), tuple(sorted(f['options'].items())))
            for f in inspector.get_foreign_keys(table)
        )
        unique = sorted(tuple(u['column_names']) for u in inspector.get_unique_constraints(table))
        # Unique indexes are not interchangeable with table constraints here:
        # partial/expression indexes can silently weaken the idempotency guard.
        unique_indexes = [i for i in inspector.get_indexes(table) if i['unique']]
        checks = sorted(''.join(c['sqltext'].lower().split()) for c in inspector.get_check_constraints(table))
        return columns, primary, foreign, unique, checks, unique_indexes

    expected_engine = create_engine('sqlite://')
    try:
        with expected_engine.begin() as expected:
            OperatorCreditRefund.__table__.create(expected)
            if signature(inspect(connection)) != signature(inspect(expected)):
                raise SchemaUpgradeError('operator_credit_refund schema is incompatible; refusing startup')
    finally:
        expected_engine.dispose()


async def validate_existing_refund_schema(target_engine) -> None:
    """Read-only preflight before any other startup migration or seeding."""
    async with target_engine.connect() as connection:
        exists = (await connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_credit_refund'"
        )).scalar()
        if exists:
            await connection.run_sync(_validate_refund_schema)


async def migrate_operator_credit_refunds(target_engine) -> bool:
    """Add the versioned reversal audit under a write lock; never backfill refunds.

    Kept separate from the transaction rebuild so earlier lifecycle migrations
    retain their no-op contract. Missing/uninitialised files stay untouched.
    """
    async with target_engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            tables = set((await connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )).scalars())
            if "transactions" not in tables:
                await connection.rollback()
                return False
            changed = "operator_credit_refund" not in tables
            await connection.run_sync(
                lambda sync: OperatorCreditRefund.__table__.create(sync, checkfirst=True)
            )
            await connection.run_sync(_validate_refund_schema)
            await connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            await connection.exec_driver_sql(
                "INSERT OR IGNORE INTO schema_migrations(version) "
                "VALUES ('slice-2.3c-operator-credit-refund')"
            )
            await connection.commit()
            return changed
        except Exception:
            await connection.rollback()
            raise


async def init_db() -> None:
    await validate_existing_refund_schema(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        statement = sqlite_insert(Setting).values(
            key="commission_rate", value="0.05", updated_at=utc_now()
        ).on_conflict_do_nothing(index_elements=[Setting.key])
        await connection.execute(statement)
    await _migrate_schema()
