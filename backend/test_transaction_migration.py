"""Slice 2.3: the guarded SQLite upgrade for existing v1.0.1/v1.0.3 databases.

The legacy schema constrained ``status`` to pending/completed/failed. A
persisted row now means "the seller committed this sale", so the table is
rebuilt with the new constraint and kept row-for-row: ids, references, amounts,
timestamps and every ``profit_ledger`` row survive untouched.
"""

import asyncio

import pytest
from sqlalchemy import URL, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import database
from backend.database import Base, Customer, ProfitLedger, Transaction, enable_sqlite_foreign_keys
from backend.migrations import migrate_transaction_lifecycle
from backend.seed import seed_data

LEGACY_TRANSACTIONS = """
CREATE TABLE transactions (
    id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL,
    ussd_string VARCHAR(255) NOT NULL,
    reference VARCHAR(255) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT transaction_status CHECK (status IN ('pending', 'completed', 'failed')),
    UNIQUE (reference),
    FOREIGN KEY(customer_id) REFERENCES customers (id),
    FOREIGN KEY(service_id) REFERENCES services (id)
)
"""


def make_engine(tmp_path, name: str = "legacy.db"):
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / name)))
    event.listen(engine.sync_engine, "connect", enable_sqlite_foreign_keys)
    return engine


async def _legacy_rows(engine) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        await seed_data(session)
        session.add(Customer(phone="0944123456", name="Ahmad"))
        await session.commit()

    async with engine.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await connection.exec_driver_sql("DROP TABLE transactions")
        await connection.exec_driver_sql(LEGACY_TRANSACTIONS)
        await connection.exec_driver_sql(
            "INSERT INTO transactions VALUES (1, 1, 1, 5000, 'pending', '*150*65477*0944123456*50#',"
            " 'TXN-legacy-pending', '2026-01-02 10:00:00')"
        )
        await connection.exec_driver_sql(
            "INSERT INTO transactions VALUES (2, 1, 1, 4000, 'completed', '*150*65477*0944123456*40#',"
            " 'TXN-legacy-completed', '2026-01-03 11:00:00')"
        )
        await connection.exec_driver_sql(
            "INSERT INTO transactions VALUES (3, 1, 1, 1000, 'failed', '*150*65477*0944123456*10#',"
            " 'TXN-legacy-failed', '2026-01-04 12:00:00')"
        )
        await connection.exec_driver_sql(
            "INSERT INTO profit_ledger (transaction_id, operator_code, amount_cents,"
            " commission_cents, profit_cents, created_at)"
            " VALUES (1, 'mtn', 5000, 250, 250, '2026-01-02 10:00:00')"
        )
        await connection.exec_driver_sql(
            "INSERT INTO profit_ledger (transaction_id, operator_code, amount_cents,"
            " commission_cents, profit_cents, created_at)"
            " VALUES (2, 'mtn', 4000, 200, 200, '2026-01-03 11:00:00')"
        )
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def build_legacy(tmp_path):
    engine = make_engine(tmp_path)
    asyncio.run(_legacy_rows(engine))
    return engine


async def _rows(engine):
    async with engine.connect() as connection:
        result = await connection.exec_driver_sql(
            "SELECT id, status, amount_cents, reference, ussd_string, created_at,"
            " legacy_status, idempotency_key, deleted_at FROM transactions ORDER BY id"
        )
        return [tuple(row) for row in result.all()]


def test_migration_maps_legacy_statuses_and_preserves_every_row(tmp_path) -> None:
    engine = build_legacy(tmp_path)
    try:
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
        rows = asyncio.run(_rows(engine))
        assert rows == [
            (1, "failed", 5000, "TXN-legacy-pending", "*150*65477*0944123456*50#",
             "2026-01-02 10:00:00", "pending", None, None),
            (2, "successful", 4000, "TXN-legacy-completed", "*150*65477*0944123456*40#",
             "2026-01-03 11:00:00", "completed", None, None),
            (3, "failed", 1000, "TXN-legacy-failed", "*150*65477*0944123456*10#",
             "2026-01-04 12:00:00", None, None, None),
        ]
    finally:
        asyncio.run(engine.dispose())


def test_migration_keeps_the_profit_ledger_and_foreign_keys(tmp_path) -> None:
    engine = build_legacy(tmp_path)
    try:
        asyncio.run(migrate_transaction_lifecycle(engine))

        async def read():
            async with engine.connect() as connection:
                ledger = await connection.exec_driver_sql(
                    "SELECT transaction_id, operator_code, amount_cents, commission_cents,"
                    " profit_cents FROM profit_ledger ORDER BY transaction_id"
                )
                fk = await connection.exec_driver_sql("PRAGMA foreign_key_check")
                return [tuple(row) for row in ledger.all()], fk.all()

        ledger, fk_violations = asyncio.run(read())
        assert ledger == [(1, "mtn", 5000, 250, 250), (2, "mtn", 4000, 200, 200)]
        assert fk_violations == []
    finally:
        asyncio.run(engine.dispose())


def test_migrated_table_enforces_the_new_constraint_and_unique_key(tmp_path) -> None:
    engine = build_legacy(tmp_path)
    try:
        asyncio.run(migrate_transaction_lifecycle(engine))

        async def check():
            async with engine.connect() as connection:
                ddl = (
                    await connection.exec_driver_sql(
                        "SELECT sql FROM sqlite_master WHERE name = 'transactions'"
                    )
                ).scalar()
                columns = {
                    row[1]
                    for row in (
                        await connection.exec_driver_sql("PRAGMA table_info(transactions)")
                    ).all()
                }
            assert "idempotency_key" in columns
            assert "deleted_at" in columns
            assert "legacy_status" in columns
            assert "'successful'" in ddl and "CHECK" in ddl
            assert "'pending'" not in ddl and "'completed'" not in ddl

            # The old vocabulary is now rejected...
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(
                        "INSERT INTO transactions (customer_id, service_id, amount_cents, status,"
                        " ussd_string, reference, created_at)"
                        " VALUES (1, 1, 900, 'pending', '*1#', 'TXN-new', '2026-02-01 00:00:00')"
                    )
            # ...the new one is accepted, and the commit key is unique.
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "INSERT INTO transactions (customer_id, service_id, amount_cents, status,"
                    " ussd_string, reference, created_at, idempotency_key)"
                    " VALUES (1, 1, 900, 'successful', '*1#', 'TXN-ok', '2026-02-01 00:00:00',"
                    " 'migratedkey00000000000001')"
                )
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(
                        "INSERT INTO transactions (customer_id, service_id, amount_cents, status,"
                        " ussd_string, reference, created_at, idempotency_key)"
                        " VALUES (1, 1, 900, 'successful', '*1#', 'TXN-dupe',"
                        " '2026-02-01 00:00:00', 'migratedkey00000000000001')"
                    )

        asyncio.run(check())
    finally:
        asyncio.run(engine.dispose())


def test_migration_is_idempotent(tmp_path) -> None:
    engine = build_legacy(tmp_path)
    try:
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is False
        assert len(asyncio.run(_rows(engine))) == 3
    finally:
        asyncio.run(engine.dispose())


def test_fresh_schema_needs_no_migration(tmp_path) -> None:
    engine = make_engine(tmp_path, "fresh.db")

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    try:
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is False
    finally:
        asyncio.run(engine.dispose())


def test_init_db_runs_the_upgrade_on_a_legacy_database(tmp_path, monkeypatch) -> None:
    """The app's own startup path (``init_db``) upgrades an old database."""
    engine = build_legacy(tmp_path)
    monkeypatch.setattr(database, "engine", engine)
    try:
        asyncio.run(database.init_db())
        rows = asyncio.run(_rows(engine))
        assert [row[1] for row in rows] == ["failed", "successful", "failed"]

        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def read_setting():
            async with sessions() as session:
                return await database.get_setting(session, "commission_rate")

        assert asyncio.run(read_setting()) == "0.05"
    finally:
        asyncio.run(engine.dispose())


def test_migrated_rows_still_flow_through_the_app(tmp_path, monkeypatch) -> None:
    """A migrated row is readable through the ORM and the live reports."""
    engine = build_legacy(tmp_path)
    try:
        asyncio.run(migrate_transaction_lifecycle(engine))
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def read():
            async with sessions() as session:
                rows = (await session.execute(select(Transaction).order_by(Transaction.id))).scalars().all()
                ledger = (await session.execute(select(ProfitLedger))).scalars().all()
                return rows, ledger

        rows, ledger = asyncio.run(read())
        assert [(row.id, row.status, row.legacy_status) for row in rows] == [
            (1, "failed", "pending"),
            (2, "successful", "completed"),
            (3, "failed", None),
        ]
        assert len(ledger) == 2
    finally:
        asyncio.run(engine.dispose())


def test_startup_upgrade_runs_for_an_existing_file(tmp_path, monkeypatch) -> None:
    """`uvicorn backend.main:app` must upgrade a database that already exists."""
    engine = build_legacy(tmp_path)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "DATABASE_URL", URL.create("sqlite+aiosqlite", database=str(tmp_path / "legacy.db"))
    )
    try:
        assert asyncio.run(database.upgrade_existing_database()) is True
        assert [row[1] for row in asyncio.run(_rows(engine))] == [
            "failed",
            "successful",
            "failed",
        ]
    finally:
        asyncio.run(engine.dispose())


def test_startup_upgrade_skips_a_missing_file(tmp_path, monkeypatch) -> None:
    """A fresh install is never created implicitly by the startup hook."""
    missing = tmp_path / "absent.db"
    monkeypatch.setattr(database, "DATABASE_URL", URL.create("sqlite+aiosqlite", database=str(missing)))
    assert asyncio.run(database.upgrade_existing_database()) is False
    assert not missing.exists()


def test_app_registers_the_startup_upgrade() -> None:
    from backend import main

    names = [getattr(handler, "__name__", "") for handler in main.app.router.on_startup]
    assert "_upgrade_schema_on_startup" in names
