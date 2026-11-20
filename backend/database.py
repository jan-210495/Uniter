from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import DDL, URL, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, event, select, text
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
    # Retained for customer/debt workflows; new POS sales are never linked.
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
    # The signed-in account that committed the sale, taken from the server-side
    # session only (never from the request). NULL for rows recorded before
    # v1.0.4: they are never back-filled. RESTRICT keeps history intact, since
    # accounts are deactivated rather than deleted. Declared last so a fresh
    # table matches one upgraded by ``ALTER TABLE ... ADD COLUMN``.
    performed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, default=None
    )
    # v1.0.4 Slice 4.1: the SIM card the sale was charged to. NULL for every
    # sale recorded before per-SIM balances: those were charged to the
    # company's legacy shop credit and are never back-filled or guessed.
    # Declared last so a fresh table matches one upgraded by ``ADD COLUMN``.
    sim_card_id: Mapped[int | None] = mapped_column(
        ForeignKey("sim_cards.id", ondelete="RESTRICT"), nullable=True, default=None
    )

    # Seller-entered per-sale snapshot, independent of the customer database.
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)

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


#: The only account roles the system supports. The first admin is created by
#: one-time setup (``/setup``); admins create sellers on the Users page.
USER_ROLES = ("admin", "seller")


class User(Base):
    """An individual sign-in account. Passwords are only ever stored hashed."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'seller')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored normalised (NFKC, trimmed, case-folded) so lookups and throttling
    # cannot be split by case or whitespace variants.
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class UserSession(Base):
    """Server-side session. Only a SHA-256 digest of the cookie value is kept.

    The CSRF token is derived from the cookie value on demand (see ``auth``),
    so no usable secret is ever written to the database.
    """

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LoginAttempt(Base):
    """A failed sign-in, keyed by a digest of the normalised username or address."""

    __tablename__ = "login_attempts"
    __table_args__ = (
        CheckConstraint("scope IN ('username', 'address')", name="ck_login_attempts_scope"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)


#: Recorded in ``schema_migrations`` once the SIM card table is installed.
SIM_SCHEMA_VERSION = "v1.0.4-sim-cards"
#: Longest SIM display name (characters, after trimming).
SIM_NAME_MAX_LENGTH = 80


class SimCard(Base):
    """One physical SIM/card account belonging to a company (v1.0.4 slice 4).

    ``operators`` stays the *company* configuration (MTN, Syriatel, with their
    services and USSD settings); a SIM card is a separate record of exactly one
    company. A private SIM has an owner account; a shared SIM has none and is
    usable by every account. SIMs are archived, never deleted, so their
    identity survives for history.

    Identity and visibility only: money stays on the company shop credit
    (``operator_balance``) until per-SIM balances are introduced, so this table
    deliberately has no balance column.
    """

    __tablename__ = "sim_cards"
    __table_args__ = (
        CheckConstraint(
            "(is_shared = 1 AND owner_user_id IS NULL) OR (is_shared = 0 AND owner_user_id IS NOT NULL)",
            name="sim_card_visibility",
        ),
        CheckConstraint(
            f"length(trim(display_name)) BETWEEN 1 AND {SIM_NAME_MAX_LENGTH}", name="sim_card_display_name"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operators.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(SIM_NAME_MAX_LENGTH), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, default=None, index=True
    )
    is_shared: Mapped[bool] = mapped_column(Boolean, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


#: Recorded in ``schema_migrations`` once per-SIM balances are installed.
SIM_BALANCE_SCHEMA_VERSION = "v1.0.4-sim-balances"
#: Largest amount SQLite can store in an INTEGER column (minor units).
MAX_MINOR_UNITS = 9223372036854775807
SIM_LEDGER_KINDS = ("topup", "sale", "refund")


class SimBalance(Base):
    """The current balance of one SIM card, in minor units (v1.0.4 slice 4.1).

    A SIM starts at 0 (a missing row reads as 0) and only changes together with
    an appended ``sim_ledger`` row in the same database transaction: a top-up
    adds, a committed sale subtracts its face value, a failed-sale refund adds
    the stored original debit back. The CHECK keeps it a non-negative integer.
    """

    __tablename__ = "sim_balances"
    __table_args__ = (
        CheckConstraint(
            "typeof(balance_cents) = 'integer' AND balance_cents >= 0", name="sim_balance_amount"
        ),
    )

    sim_card_id: Mapped[int] = mapped_column(
        ForeignKey("sim_cards.id", ondelete="RESTRICT"), primary_key=True, autoincrement=False
    )
    balance_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class SimLedger(Base):
    """Append-only audit of every per-SIM money movement (v1.0.4 slice 4.1).

    * ``topup``: credit loaded onto the SIM; carries the client's idempotency
      key so a repeated submit never credits twice;
    * ``sale``: the face-value debit of one committed transaction;
    * ``refund``: the stored original debit returned when that sale is marked
      failed.

    ``(transaction_id, kind)`` is unique, so a sale is debited and refunded at
    most once. Every row records who did it, the amount, the time, a unique
    reference and the SIM balance that resulted. Triggers refuse UPDATE and
    DELETE, so history can only grow.
    """

    __tablename__ = "sim_ledger"
    __table_args__ = (
        CheckConstraint("kind IN ('topup', 'sale', 'refund')", name="sim_ledger_kind"),
        CheckConstraint(
            "typeof(amount_cents) = 'integer' AND amount_cents >= 0", name="sim_ledger_amount"
        ),
        CheckConstraint(
            "typeof(balance_after_cents) = 'integer' AND balance_after_cents >= 0",
            name="sim_ledger_balance_after",
        ),
        CheckConstraint(
            "(kind = 'topup' AND transaction_id IS NULL AND idempotency_key IS NOT NULL AND amount_cents > 0)"
            " OR (kind IN ('sale', 'refund') AND transaction_id IS NOT NULL AND idempotency_key IS NULL)",
            name="sim_ledger_link",
        ),
        UniqueConstraint("transaction_id", "kind", name="sim_ledger_transaction_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sim_card_id: Mapped[int] = mapped_column(
        ForeignKey("sim_cards.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=True, default=None
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, default=None)
    reference: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    performed_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


#: Append-only guards installed with ``sim_ledger`` (validated on startup).
SIM_LEDGER_TRIGGERS = {
    "sim_ledger_no_update": (
        "CREATE TRIGGER sim_ledger_no_update BEFORE UPDATE ON sim_ledger "
        "BEGIN SELECT RAISE(ABORT, 'sim_ledger is append-only'); END"
    ),
    "sim_ledger_no_delete": (
        "CREATE TRIGGER sim_ledger_no_delete BEFORE DELETE ON sim_ledger "
        "BEGIN SELECT RAISE(ABORT, 'sim_ledger is append-only'); END"
    ),
}
for _trigger_sql in SIM_LEDGER_TRIGGERS.values():
    event.listen(SimLedger.__table__, "after_create", DDL(_trigger_sql))


class SimDefault(Base):
    """Each account's preferred SIM card per company (one row per pair).

    Only ever points at an active SIM card the account may use; archiving a
    SIM or changing who may use it removes the defaults that would no longer
    be allowed, and readers re-check permission anyway.
    """

    __tablename__ = "sim_defaults"
    __table_args__ = (UniqueConstraint("user_id", "operator_id", name="sim_default_user_company"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id", ondelete="RESTRICT"), nullable=False)
    sim_card_id: Mapped[int] = mapped_column(
        ForeignKey("sim_cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


SIM_BALANCE_TABLES = (SimBalance, SimLedger, SimDefault)
#: ``transactions.sim_card_id`` exactly as this build creates it.
SIM_COLUMN = "sim_card_id"
SIM_COLUMN_SQL = "sim_card_id INTEGER REFERENCES sim_cards (id) ON DELETE RESTRICT"
SIM_COLUMN_FOREIGN_KEY = ("sim_cards", "id", "NO ACTION", "RESTRICT", "NONE")


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
    await _migrate_auth(engine)
    await migrate_sim_cards(engine)
    await migrate_sim_balances(engine)


async def _migrate_auth(target_engine) -> bool:
    """Install the auth tables and record initialisation (guarded; never creates accounts)."""
    try:
        from .auth import migrate_auth_schema
    except ImportError:  # pragma: no cover - flat module layout (PyInstaller)
        from auth import migrate_auth_schema

    return await migrate_auth_schema(target_engine)


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
        await validate_existing_sim_schema(engine)
        await validate_existing_sim_balance_schema(engine)
    changed = await _upgrade(engine, DATABASE_URL.database)
    if DATABASE_URL.database and os.path.exists(DATABASE_URL.database):
        changed = await migrate_operator_credit_refunds(engine) or changed
        # Runs only after every schema safety check above has passed; it is a
        # no-op for uninitialised files and never creates or changes accounts.
        changed = await _migrate_auth(engine) or changed
        # Needs the users table, so it runs after the auth step.
        changed = await migrate_sim_cards(engine) or changed
        # Needs sim_cards, so it runs last.
        changed = await migrate_sim_balances(engine) or changed
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


def _validate_sim_schema(connection) -> None:
    """Fail closed on a preexisting ``sim_cards`` table that differs from ours.

    Compares columns, keys, foreign keys (with their ON DELETE rules), unique
    constraints, checks and indexes against a scratch copy created from the
    model, so a table that cannot enforce the visibility/owner invariants is
    never used.
    """
    from sqlalchemy import create_engine, inspect
    try:
        from .migrations import SchemaUpgradeError
    except ImportError:  # pragma: no cover - frozen flat layout
        from migrations import SchemaUpgradeError

    table = SimCard.__tablename__

    def signature(inspector):
        columns = tuple(
            (c["name"], str(c["type"]), c["nullable"], c["default"]) for c in inspector.get_columns(table)
        )
        primary = tuple(inspector.get_pk_constraint(table)["constrained_columns"])
        foreign = sorted(
            (tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]),
             tuple(sorted(f["options"].items())))
            for f in inspector.get_foreign_keys(table)
        )
        unique = sorted(tuple(u["column_names"]) for u in inspector.get_unique_constraints(table))
        indexes = sorted((i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in inspector.get_indexes(table))
        checks = sorted("".join(c["sqltext"].lower().split()) for c in inspector.get_check_constraints(table))
        return columns, primary, foreign, unique, indexes, checks

    expected_engine = create_engine("sqlite://")
    try:
        with expected_engine.begin() as expected:
            SimCard.__table__.create(expected)
            if signature(inspect(connection)) != signature(inspect(expected)):
                raise SchemaUpgradeError("sim_cards schema is incompatible; refusing startup")
    finally:
        expected_engine.dispose()


async def validate_existing_sim_schema(target_engine) -> None:
    """Read-only preflight, run before any startup step writes to the file."""
    async with target_engine.connect() as connection:
        exists = (await connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sim_cards'"
        )).scalar()
        if exists:
            await connection.run_sync(_validate_sim_schema)


async def migrate_sim_cards(target_engine) -> bool:
    """Install the SIM card table under a write lock; never creates a SIM.

    Same guarded contract as the other startup steps: uninitialised files are
    left untouched, an incompatible existing table fails closed, and the step
    commits atomically or not at all. Existing money rows are not read or
    moved: balances, top-ups, refunds and sales stay with their company.
    """
    try:
        from .migrations import SchemaUpgradeError
    except ImportError:  # pragma: no cover - frozen flat layout
        from migrations import SchemaUpgradeError

    async with target_engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            tables = set((await connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )).scalars())
            if "transactions" not in tables:
                await connection.rollback()
                return False
            if not {"operators", "users"} <= tables:
                raise SchemaUpgradeError("operators/users tables are missing; refusing to add SIM cards")
            changed = "sim_cards" not in tables
            await connection.run_sync(lambda sync: SimCard.__table__.create(sync, checkfirst=True))
            await connection.run_sync(_validate_sim_schema)
            await connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            await connection.execute(
                text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (:version)"),
                {"version": SIM_SCHEMA_VERSION},
            )
            await connection.commit()
            return changed
        except Exception:
            await connection.rollback()
            raise


def _sim_balance_problems(connection) -> tuple[list[str], bool]:
    """Read-only check of the Slice 4.1 schema; returns ``(missing, marked)``.

    Every part that exists must match this build exactly (columns, keys,
    foreign keys with their ON DELETE rules, unique constraints, checks,
    indexes, the append-only triggers and the shape of
    ``transactions.sim_card_id``); otherwise ``SchemaUpgradeError`` is raised
    so the caller fails closed. ``missing`` lists the parts still to install.
    """
    from sqlalchemy import create_engine, inspect
    try:
        from .migrations import SchemaUpgradeError
    except ImportError:  # pragma: no cover - frozen flat layout
        from migrations import SchemaUpgradeError

    def signature(inspector, table):
        columns = tuple(
            (c["name"], str(c["type"]), c["nullable"], c["default"]) for c in inspector.get_columns(table)
        )
        primary = tuple(inspector.get_pk_constraint(table)["constrained_columns"])
        foreign = sorted(
            (tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]),
             tuple(sorted(f["options"].items())))
            for f in inspector.get_foreign_keys(table)
        )
        unique = sorted(tuple(u["column_names"]) for u in inspector.get_unique_constraints(table))
        indexes = sorted(
            (i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in inspector.get_indexes(table)
        )
        checks = sorted("".join(c["sqltext"].lower().split()) for c in inspector.get_check_constraints(table))
        return columns, primary, foreign, unique, indexes, checks

    def triggers(sync, table):
        return sorted(
            (str(name), " ".join(str(sql).split()).lower())
            for name, sql in sync.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
            ).all()
        )

    present = set(inspect(connection).get_table_names())
    missing = [model.__tablename__ for model in SIM_BALANCE_TABLES if model.__tablename__ not in present]
    if any(model.__tablename__ in present for model in SIM_BALANCE_TABLES):
        expected_engine = create_engine("sqlite://")
        try:
            with expected_engine.begin() as expected:
                for model in SIM_BALANCE_TABLES:
                    model.__table__.create(expected)
                for model in SIM_BALANCE_TABLES:
                    table = model.__tablename__
                    if table not in present:
                        continue
                    if signature(inspect(connection), table) != signature(inspect(expected), table):
                        raise SchemaUpgradeError(f"{table} schema is incompatible; refusing startup")
                    if triggers(connection, table) != triggers(expected, table):
                        raise SchemaUpgradeError(f"{table} triggers are incompatible; refusing startup")
        finally:
            expected_engine.dispose()

    if "transactions" in present:
        columns = {
            row[1]: row for row in connection.exec_driver_sql("PRAGMA table_info(transactions)").all()
        }
        column = columns.get(SIM_COLUMN)
        if column is None:
            missing.append(f"transactions.{SIM_COLUMN}")
        else:
            _cid, _name, sql_type, notnull, default, primary_key = column
            foreign_keys = [
                (str(table), str(target), str(on_update).upper(), str(on_delete).upper(), str(match).upper())
                for _id, _seq, table, source, target, on_update, on_delete, match in connection.exec_driver_sql(
                    "PRAGMA foreign_key_list(transactions)"
                ).all()
                if source == SIM_COLUMN
            ]
            if (
                str(sql_type).upper() != "INTEGER"
                or notnull
                or default is not None
                or primary_key
                or foreign_keys != [SIM_COLUMN_FOREIGN_KEY]
            ):
                raise SchemaUpgradeError(
                    f"transactions.{SIM_COLUMN} exists with an unexpected definition; refusing startup"
                )

    marked = False
    if "schema_migrations" in present:
        marked = bool(connection.exec_driver_sql(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (SIM_BALANCE_SCHEMA_VERSION,)
        ).scalar())
    if marked and missing:
        raise SchemaUpgradeError(
            "per-SIM balance schema is marked installed but incomplete; refusing startup"
        )
    return missing, marked


async def validate_existing_sim_balance_schema(target_engine) -> None:
    """Read-only preflight, run before any startup step writes to the file."""
    async with target_engine.connect() as connection:
        await connection.run_sync(_sim_balance_problems)


async def migrate_sim_balances(target_engine) -> bool:
    """Install per-SIM balances under a write lock; moves no money at all.

    Creates ``sim_balances``, ``sim_ledger`` (with its append-only triggers)
    and ``sim_defaults``, and adds the nullable ``transactions.sim_card_id``
    in place. Existing rows are never rewritten: legacy sales keep NULL (they
    were charged to the company's legacy shop credit), no SIM is created and
    no balance, top-up or refund is copied, split or moved. Uninitialised
    files are left untouched, an incompatible schema fails closed, and the
    step commits atomically or not at all.
    """
    try:
        from .migrations import SchemaUpgradeError
    except ImportError:  # pragma: no cover - frozen flat layout
        from migrations import SchemaUpgradeError

    async with target_engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            tables = set((await connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )).scalars())
            if "transactions" not in tables:
                await connection.rollback()
                return False
            if not {"operators", "users", "sim_cards"} <= tables:
                raise SchemaUpgradeError("operators/users/sim_cards are missing; refusing to add SIM balances")
            missing, _marked = await connection.run_sync(_sim_balance_problems)
            changed = bool(missing)
            for model in SIM_BALANCE_TABLES:
                await connection.run_sync(lambda sync, m=model: m.__table__.create(sync, checkfirst=True))
            if f"transactions.{SIM_COLUMN}" in missing:
                count_sql = "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) FROM transactions"
                before = (await connection.exec_driver_sql(count_sql)).one()
                await connection.exec_driver_sql(f"ALTER TABLE transactions ADD COLUMN {SIM_COLUMN_SQL}")
                after = (await connection.exec_driver_sql(count_sql)).one()
                filled = (await connection.exec_driver_sql(
                    f"SELECT COUNT(*) FROM transactions WHERE {SIM_COLUMN} IS NOT NULL"
                )).scalar()
                if tuple(after) != tuple(before) or filled:
                    raise SchemaUpgradeError("adding transactions.sim_card_id changed existing rows")
            remaining, _marked = await connection.run_sync(_sim_balance_problems)
            if remaining:
                raise SchemaUpgradeError("per-SIM balance schema could not be installed")
            await connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            await connection.execute(
                text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (:version)"),
                {"version": SIM_BALANCE_SCHEMA_VERSION},
            )
            await connection.commit()
            return changed
        except Exception:
            await connection.rollback()
            raise


async def init_db() -> None:
    await validate_existing_refund_schema(engine)
    await validate_existing_sim_schema(engine)
    await validate_existing_sim_balance_schema(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        statement = sqlite_insert(Setting).values(
            key="commission_rate", value="0.05", updated_at=utc_now()
        ).on_conflict_do_nothing(index_elements=[Setting.key])
        await connection.execute(statement)
    await _migrate_schema()
