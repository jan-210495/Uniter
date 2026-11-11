"""Guarded SQLite upgrade for existing v1.0.1/v1.0.3 databases.

The legacy schema constrained ``status`` to pending/completed/failed. A
persisted row now means "the seller committed this sale", so the table is
rebuilt with the new constraint and kept row-for-row: ids, references, amounts,
timestamps and every ``profit_ledger`` row survive untouched, and the previous
status of each row is recorded in ``legacy_status``.

The rebuild reads the live ``CREATE TABLE`` statement instead of assuming the
minimal fixture below, so these tests also cover a hostile-but-realistic
database with custom/expression/partial indexes, a trigger, a view, an inbound
foreign key, an extra column, ``WITHOUT ROWID``, unknown statuses, injected
failures and two concurrent starts.
"""

import asyncio

import pytest
from sqlalchemy import URL, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import database
from backend.database import Base, Customer, ProfitLedger, Transaction, enable_sqlite_foreign_keys
from backend.migrations import SchemaUpgradeError, migrate_transaction_lifecycle
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
             "2026-01-04 12:00:00", "failed", None, None),
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
        # The previous status of every imported row is recorded, so the mapping
        # stays traceable (and reversible) for all three legacy values.
        assert [(row.id, row.status, row.legacy_status) for row in rows] == [
            (1, "failed", "pending"),
            (2, "successful", "completed"),
            (3, "failed", "failed"),
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


# ---------------------------------------------------------------------------
# Hardened upgrade: the live schema is preserved, not assumed
# ---------------------------------------------------------------------------

#: A realistic v1.0.x database that is *not* the minimal fixture above: custom,
#: expression and partial indexes, a trigger, a view, an inbound foreign key and
#: an extra audit table that references transactions.
HOSTILE_LEGACY_SQL = (
    """
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
    """,
    "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, event TEXT, reference TEXT)",
    "CREATE INDEX ix_transactions_status_created ON transactions (status, created_at)",
    "CREATE UNIQUE INDEX ux_transactions_reference_lower ON transactions (lower(reference))",
    "CREATE INDEX ix_transactions_pending_created ON transactions (created_at)"
    " WHERE status = 'pending'",
    """
    CREATE TRIGGER trg_transactions_insert AFTER INSERT ON transactions BEGIN
        INSERT INTO audit_log (event, reference) VALUES ('insert', NEW.reference);
    END
    """,
    """
    CREATE VIEW v_transactions_month AS
        SELECT substr(created_at, 1, 7) AS month,
               sum(amount_cents) AS total_cents,
               count(*) AS deals
        FROM transactions
        GROUP BY month
    """,
    """
    CREATE TABLE transaction_audit (
        id INTEGER NOT NULL PRIMARY KEY,
        transaction_id INTEGER NOT NULL REFERENCES transactions (id),
        note VARCHAR(255)
    )
    """,
    "INSERT INTO transactions VALUES (1, 1, 1, 5000, 'pending', '*150*1#', 'TXN-pending',"
    " '2026-01-02 10:00:00')",
    "INSERT INTO transactions VALUES (2, 1, 1, 4000, 'completed', '*150*40#', 'TXN-completed',"
    " '2026-01-03 11:00:00')",
    "INSERT INTO transactions VALUES (3, 1, 1, 1000, 'failed', '*150*10#', 'TXN-failed',"
    " '2026-01-04 12:00:00')",
    "INSERT INTO transaction_audit VALUES (1, 2, 'kept')",
    "INSERT INTO profit_ledger (transaction_id, operator_code, amount_cents, commission_cents,"
    " profit_cents, created_at) VALUES (2, 'mtn', 4000, 200, 200, '2026-01-03 11:00:00')",
)


async def _install_legacy(connection, statements) -> None:
    await connection.execution_options(isolation_level="AUTOCOMMIT")
    await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    await connection.exec_driver_sql("DROP TABLE transactions")
    for statement in statements:
        await connection.exec_driver_sql(statement)
    await connection.exec_driver_sql("PRAGMA foreign_keys=ON")


async def _build_with(engine, statements) -> None:
    """Create the current schema, seed it, then swap in a legacy transactions table."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        await seed_data(session)
        session.add(Customer(phone="0944123456", name="Ahmad"))
        await session.commit()
    async with engine.connect() as connection:
        await _install_legacy(connection, statements)


def build_hostile_legacy(tmp_path):
    engine = make_engine(tmp_path, "hostile.db")
    asyncio.run(_build_with(engine, HOSTILE_LEGACY_SQL))
    return engine


def _snapshot(connection) -> dict:
    """Everything an upgrade must preserve, or change deliberately."""
    names = {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).all()
    }

    return {
        "objects": [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
                " WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).all()
        ],
        "columns": [
            tuple(row) for row in connection.exec_driver_sql("PRAGMA table_info(transactions)").all()
        ],
        "indexes": sorted(
            tuple(row) for row in connection.exec_driver_sql("PRAGMA index_list(transactions)").all()
        ),
        "foreign_keys": sorted(
            tuple(row)
            for row in connection.exec_driver_sql("PRAGMA foreign_key_list(transactions)").all()
        ),
        "index_columns": sorted(
            (
                tuple(row)[3],
                tuple(
                    column[2] or ""
                    for column in connection.exec_driver_sql(
                        f"PRAGMA index_info({tuple(row)[1]})"
                    ).all()
                ),
            )
            for row in connection.exec_driver_sql("PRAGMA index_list(transactions)").all()
        ),
        "rows": [
            tuple(row)
            for row in connection.exec_driver_sql("SELECT * FROM transactions ORDER BY id").all()
        ],
        "ledger": [
            tuple(row)
            for row in connection.exec_driver_sql("SELECT * FROM profit_ledger ORDER BY id").all()
        ],
        "balances": [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT * FROM operator_balance ORDER BY id"
            ).all()
        ],
        "audit": [
            tuple(row) for row in connection.exec_driver_sql("SELECT * FROM audit_log ORDER BY id").all()
        ]
        if "audit_log" in names
        else [],
        "view": [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT * FROM v_transactions_month ORDER BY month"
            ).all()
        ]
        if "v_transactions_month" in names
        else [],
        "foreign_key_check": [
            tuple(row) for row in connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        ],
    }


async def _read_snapshot(engine) -> dict:
    async with engine.connect() as connection:
        return await connection.run_sync(_snapshot)


def _objects_by_key(snapshot) -> dict:
    return {(kind, name, table): sql for kind, name, table, sql in snapshot["objects"]}


def test_upgrade_preserves_indexes_triggers_views_and_inbound_foreign_keys(tmp_path) -> None:
    engine = build_hostile_legacy(tmp_path)
    try:
        before = asyncio.run(_read_snapshot(engine))
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
        after = asyncio.run(_read_snapshot(engine))

        # Everything except the transactions table itself is byte-identical.
        before_objects = _objects_by_key(before)
        after_objects = _objects_by_key(after)
        assert set(before_objects) == set(after_objects)
        for key in before_objects:
            if key != ("table", "transactions", "transactions"):
                assert before_objects[key] == after_objects[key], key
                assert before_objects[key] == after_objects[key], key

        # The custom indexes and the trigger were recreated from their own SQL.
        for name in (
            "ix_transactions_status_created",
            "ux_transactions_reference_lower",
            "ix_transactions_pending_created",
        ):
            assert after_objects[("index", name, "transactions")] == before_objects[
                ("index", name, "transactions")
            ]
        assert after_objects[("trigger", "trg_transactions_insert", "transactions")] == (
            before_objects[("trigger", "trg_transactions_insert", "transactions")]
        )

        # The view still resolves against the rebuilt table and keeps its totals.
        assert after["view"] == before["view"] == [("2026-01", 10000, 3)]
        # The inbound foreign key still points at transactions and is valid.
        assert after["foreign_key_check"] == []
        assert after["audit"] == before["audit"] == [
            (1, "insert", "TXN-pending"),
            (2, "insert", "TXN-completed"),
            (3, "insert", "TXN-failed"),
        ]
        # The copy did not re-fire the table's own trigger.
        assert len(after["audit"]) == 3
        assert after["ledger"] == before["ledger"]
        assert after["balances"] == before["balances"]
        assert [row[:4] for row in after["rows"]] == [row[:4] for row in before["rows"]]
        assert [row[4] for row in after["rows"]] == ["failed", "successful", "failed"]
        assert [row[7] for row in after["rows"]] == [row[7] for row in before["rows"]]

        async def insert_new_sale() -> None:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "INSERT INTO transactions (customer_id, service_id, amount_cents, status,"
                    " ussd_string, reference, created_at)"
                    " VALUES (1, 1, 900, 'successful', '*150*9#', 'TXN-after',"
                    " '2026-02-01 00:00:00')"
                )

        asyncio.run(insert_new_sale())
        after_reload = asyncio.run(_read_snapshot(engine))
        assert after_reload["audit"][-1] == (4, "insert", "TXN-after")

        # The partial index is still a partial index on the same predicate.
        index_sql = _objects_by_key(after_reload)[
            ("index", "ix_transactions_pending_created", "transactions")
        ]
        assert "WHERE status = 'pending'" in index_sql

        # The expression index is still an expression index.
        expression_index = _objects_by_key(after_reload)[
            ("index", "ux_transactions_reference_lower", "transactions")
        ]
        assert "lower(reference)" in expression_index
    finally:
        asyncio.run(engine.dispose())


def test_upgraded_schema_matches_a_freshly_created_database(tmp_path) -> None:
    """Migration produces the schema a new install gets, for the app's columns."""
    engine = build_legacy(tmp_path)
    fresh = make_engine(tmp_path, "fresh.db")

    async def setup_fresh() -> None:
        async with fresh.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(setup_fresh())
    try:
        asyncio.run(migrate_transaction_lifecycle(engine))
        migrated = asyncio.run(_read_snapshot(engine))
        created = asyncio.run(_read_snapshot(fresh))
    finally:
        asyncio.run(engine.dispose())
        asyncio.run(fresh.dispose())

    # Column order, types, NOT NULL, DEFAULTs and primary keys match exactly.
    assert migrated["columns"] == created["columns"]
    # Same foreign keys, same unique columns, same index shapes (autoindex names
    # may be numbered differently, so they are compared by uniqueness/columns).
    assert migrated["foreign_keys"] == created["foreign_keys"]
    assert migrated["index_columns"] == created["index_columns"]
    assert sorted(entry[2] for entry in migrated["indexes"]) == sorted(
        entry[2] for entry in created["indexes"]
    )


LEGACY_DDL_TEMPLATE = """
CREATE TABLE transactions (
    id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL,
    ussd_string VARCHAR(255) NOT NULL,
    reference VARCHAR(255) NOT NULL,
    created_at DATETIME NOT NULL,
    {extra_columns}
    PRIMARY KEY (id),
    {extra_constraints}
    FOREIGN KEY(customer_id) REFERENCES customers (id),
    FOREIGN KEY(service_id) REFERENCES services (id),
    UNIQUE (reference)
){trailer}
"""


def legacy_ddl(extra_columns="", extra_constraints="", trailer="", check=True) -> str:
    constraints = "CONSTRAINT transaction_status CHECK (status IN ('pending', 'completed', 'failed')),"
    return LEGACY_DDL_TEMPLATE.format(
        extra_columns=extra_columns,
        extra_constraints=(constraints if check else "") + extra_constraints,
        trailer=trailer,
    )


def test_upgrade_preserves_extra_columns_and_a_without_rowid_table(tmp_path) -> None:
    """Table options and columns the app does not know about are carried over."""
    ddl = legacy_ddl(extra_columns="    note VARCHAR(255),", trailer=" WITHOUT ROWID")
    engine = make_engine(tmp_path, "without-rowid.db")

    async def setup() -> None:
        await _build_with(
            engine,
            (
                ddl,
                "INSERT INTO transactions (id, customer_id, service_id, amount_cents, status,"
                " ussd_string, reference, created_at, note)"
                " VALUES (1, 1, 1, 5000, 'completed', '*150*1#', 'TXN-1',"
                " '2026-01-02 10:00:00', 'kept')",
            ),
        )

    asyncio.run(setup())
    try:
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
        snapshot = asyncio.run(_read_snapshot(engine))
        ddl_after = _objects_by_key(snapshot)[("table", "transactions", "transactions")]
        assert "WITHOUT ROWID" in ddl_after
        assert "note VARCHAR(255)" in ddl_after
        assert snapshot["rows"][0][8] == "kept"  # the extra column kept its value
        assert snapshot["rows"][0][4] == "successful"
    finally:
        asyncio.run(engine.dispose())


def test_upgrade_aborts_on_an_unknown_legacy_status(tmp_path) -> None:
    """An unexpected status is not guessed at: nothing is replaced."""
    engine = make_engine(tmp_path, "unknown-status.db")

    async def setup() -> None:
        await _build_with(
            engine,
            (
                legacy_ddl(check=False),
                "INSERT INTO transactions VALUES (1, 1, 1, 5000, 'refunded', '*150*1#',"
                " 'TXN-1', '2026-01-02 10:00:00')",
            ),
        )

    asyncio.run(setup())
    try:
        before = asyncio.run(_read_snapshot(engine))
        with pytest.raises(SchemaUpgradeError) as error:
            asyncio.run(migrate_transaction_lifecycle(engine))
        assert "refunded" in str(error.value)
        after = asyncio.run(_read_snapshot(engine))
        assert after == before
    finally:
        asyncio.run(engine.dispose())


def test_upgrade_aborts_on_an_unknown_not_null_column(tmp_path) -> None:
    engine = make_engine(tmp_path, "unknown-column.db")

    async def setup() -> None:
        await _build_with(
            engine,
            (
                legacy_ddl(extra_columns="    operator_note VARCHAR(20) NOT NULL,"),
                "INSERT INTO transactions VALUES (1, 1, 1, 5000, 'completed', '*150*1#',"
                " 'TXN-1', '2026-01-02 10:00:00', 'x')",
            ),
        )

    asyncio.run(setup())
    try:
        before = asyncio.run(_read_snapshot(engine))
        with pytest.raises(SchemaUpgradeError) as error:
            asyncio.run(migrate_transaction_lifecycle(engine))
        assert "operator_note" in str(error.value)
        assert asyncio.run(_read_snapshot(engine)) == before
    finally:
        asyncio.run(engine.dispose())


def test_upgrade_aborts_on_a_partially_upgraded_table(tmp_path) -> None:
    engine = make_engine(tmp_path, "partial-columns.db")

    async def setup() -> None:
        await _build_with(
            engine,
            (
                legacy_ddl(extra_columns="    idempotency_key VARCHAR(64),"),
                "INSERT INTO transactions (id, customer_id, service_id, amount_cents, status,"
                " ussd_string, reference, created_at, idempotency_key)"
                " VALUES (1, 1, 1, 5000, 'completed', '*150*1#', 'TXN-1',"
                " '2026-01-02 10:00:00', NULL)",
            ),
        )

    asyncio.run(setup())
    try:
        before = asyncio.run(_read_snapshot(engine))
        with pytest.raises(SchemaUpgradeError) as error:
            asyncio.run(migrate_transaction_lifecycle(engine))
        assert "idempotency_key" in str(error.value)
        assert asyncio.run(_read_snapshot(engine)) == before
    finally:
        asyncio.run(engine.dispose())


def test_a_failed_step_rolls_back_and_leaves_the_original_database(tmp_path, monkeypatch) -> None:
    """Fail between copying the rows and replacing the table: nothing changes."""
    from backend import migrations

    engine = build_hostile_legacy(tmp_path)
    try:
        before = asyncio.run(_read_snapshot(engine))

        def explode(connection, plan):
            raise RuntimeError("injected failure between copy and rename")

        monkeypatch.setattr(migrations, "_verify_copy", explode)
        with pytest.raises(RuntimeError, match="injected failure"):
            asyncio.run(migrate_transaction_lifecycle(engine))
        monkeypatch.undo()

        after = asyncio.run(_read_snapshot(engine))
        assert after == before
        assert not any(
            name == "transactions_legacy" for _kind, name, _table, _sql in after["objects"]
        )

        # The database is still on the old schema and still upgradeable.
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
        migrated = asyncio.run(_read_snapshot(engine))
        assert [row[4] for row in migrated["rows"]] == ["failed", "successful", "failed"]
        assert migrated["foreign_key_check"] == []
    finally:
        asyncio.run(engine.dispose())


def test_two_concurrent_upgrades_keep_the_database_intact(tmp_path) -> None:
    engine = build_hostile_legacy(tmp_path)
    second = make_engine(tmp_path, "hostile.db")
    try:
        before = asyncio.run(_read_snapshot(engine))

        async def both():
            return await asyncio.gather(
                migrate_transaction_lifecycle(engine),
                migrate_transaction_lifecycle(second),
            )

        results = asyncio.run(both())
        assert sorted(results) == [False, True]
        after = asyncio.run(_read_snapshot(engine))
        assert after["foreign_key_check"] == []
        assert [row[4] for row in after["rows"]] == ["failed", "successful", "failed"]
        assert [row[:4] for row in after["rows"]] == [row[:4] for row in before["rows"]]
        assert after["ledger"] == before["ledger"]
        assert not any(
            name == "transactions_legacy" for _kind, name, _table, _sql in after["objects"]
        )
        assert _objects_by_key(after)[("view", "v_transactions_month", "v_transactions_month")] == (
            _objects_by_key(before)[("view", "v_transactions_month", "v_transactions_month")]
        )
    finally:
        asyncio.run(engine.dispose())
        asyncio.run(second.dispose())


def test_foreign_keys_are_enforced_for_app_connections_after_the_upgrade(tmp_path) -> None:
    engine = build_hostile_legacy(tmp_path)

    async def check() -> None:
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
            assert (await connection.exec_driver_sql("PRAGMA legacy_alter_table")).scalar() in (0, None)
            violations = (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
            assert violations == []
            # Dependent tables are still reachable through the rebuilt table.
            joined = (
                await connection.exec_driver_sql(
                    "SELECT t.id, t.status, p.profit_cents FROM transactions t"
                    " JOIN profit_ledger p ON p.transaction_id = t.id ORDER BY t.id"
                )
            ).all()
            assert [tuple(row) for row in joined] == [(2, "successful", 200)]
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "INSERT INTO transactions (customer_id, service_id, amount_cents, status,"
                    " ussd_string, reference, created_at)"
                    " VALUES (999, 1, 900, 'failed', '*150*9#', 'TXN-orphan',"
                    " '2026-02-01 00:00:00')"
                )

    try:
        asyncio.run(migrate_transaction_lifecycle(engine))
        asyncio.run(check())
    finally:
        asyncio.run(engine.dispose())


def test_startup_upgrade_reports_an_empty_file_without_creating_tables(tmp_path, monkeypatch) -> None:
    path = tmp_path / "empty.db"
    path.touch()
    engine = make_engine(tmp_path, "empty.db")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "DATABASE_URL", URL.create("sqlite+aiosqlite", database=str(path))
    )
    try:
        assert asyncio.run(database.upgrade_existing_database()) is False

        async def tables():
            async with engine.connect() as connection:
                result = await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
                return result.all()

        assert asyncio.run(tables()) == []
    finally:
        asyncio.run(engine.dispose())


def test_startup_upgrade_fails_closed_on_a_partially_initialised_schema(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "partial.db"
    engine = make_engine(tmp_path, "partial.db")

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE customers (id INTEGER PRIMARY KEY, phone VARCHAR(32) NOT NULL)"
            )

    asyncio.run(setup())
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "DATABASE_URL", URL.create("sqlite+aiosqlite", database=str(path))
    )
    try:
        with pytest.raises(SchemaUpgradeError):
            asyncio.run(database.upgrade_existing_database())

        # Nothing was created or migrated behind the operator's back.
        async def tables():
            async with engine.connect() as connection:
                result = await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
                return [row[0] for row in result.all()]

        assert asyncio.run(tables()) == ["customers"]
    finally:
        asyncio.run(engine.dispose())


def test_upgrade_aborts_when_the_rename_cannot_be_kept_local(tmp_path, monkeypatch) -> None:
    """Old SQLite versions ignore ``legacy_alter_table``: detect it and stop.

    Without the pragma a rename rewrites every other object that names
    ``transactions`` (views, triggers, and the foreign key in ``profit_ledger``),
    which would leave the database pointing at a table that no longer exists.
    The upgrade verifies the surrounding schema and rolls back instead.
    """
    from backend import migrations

    engine = build_hostile_legacy(tmp_path)
    monkeypatch.setattr(migrations, "PRAGMA_LEGACY_ALTER_ON", "PRAGMA legacy_alter_table=OFF")
    try:
        before = asyncio.run(_read_snapshot(engine))
        with pytest.raises(SchemaUpgradeError, match="schema objects changed"):
            asyncio.run(migrate_transaction_lifecycle(engine))
        assert asyncio.run(_read_snapshot(engine)) == before
    finally:
        asyncio.run(engine.dispose())


def test_desktop_seed_path_upgrades_a_legacy_database(tmp_path, monkeypatch) -> None:
    """The packaged desktop app seeds through ``desktop._seed_database``."""
    from backend import desktop

    engine = build_legacy(tmp_path)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(desktop, "engine", engine)
    monkeypatch.setattr(desktop, "SessionLocal", sessions)
    try:
        asyncio.run(desktop._seed_database())
        rows = asyncio.run(_rows(engine))
        assert [row[1] for row in rows] == ["failed", "successful", "failed"]
        assert [row[6] for row in rows] == ["pending", "completed", "failed"]

        # Every launch re-runs it: the upgrade is a no-op the second time.
        asyncio.run(desktop._seed_database())
        assert asyncio.run(_rows(engine)) == rows
    finally:
        asyncio.run(engine.dispose())


def test_desktop_refuses_to_start_when_the_upgrade_is_unsafe(tmp_path, monkeypatch) -> None:
    from backend import desktop

    def boom():
        raise SchemaUpgradeError("transactions table has unexpected status values: refunded")

    monkeypatch.setattr(desktop, "ensure_data_dir", lambda *args, **kwargs: tmp_path / "uniter.db")
    monkeypatch.setattr(desktop, "ensure_default_data", boom)
    assert desktop.main(["--no-browser", "--no-tray"]) == 1


INLINE_CHECK_LEGACY = """
CREATE TABLE transactions (
    id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('pending', 'completed', 'failed')),
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

DEFAULT_PENDING_LEGACY = """
CREATE TABLE transactions (
    id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
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


def test_every_status_check_is_rewritten(tmp_path) -> None:
    """A duplicate CHECK (column-level and table-level) cannot survive."""
    engine = make_engine(tmp_path, "inline-check.db")

    async def setup() -> None:
        await _build_with(
            engine,
            (
                INLINE_CHECK_LEGACY,
                "INSERT INTO transactions VALUES (1, 1, 1, 5000, 'completed', '*150*1#',"
                " 'TXN-1', '2026-01-02 10:00:00')",
            ),
        )

    asyncio.run(setup())
    try:
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
        ddl = _objects_by_key(asyncio.run(_read_snapshot(engine)))[
            ("table", "transactions", "transactions")
        ]
        assert "'pending'" not in ddl and "'completed'" not in ddl
        assert ddl.count("'successful'") >= 2  # both checks were rewritten

        async def accepts(status: str) -> bool:
            async with engine.begin() as connection:
                try:
                    await connection.exec_driver_sql(
                        "INSERT INTO transactions (customer_id, service_id, amount_cents,"
                        " status, ussd_string, reference, created_at)"
                        f" VALUES (1, 1, 900, '{status}', '*150*9#', 'TXN-{status}',"
                        " '2026-02-01 00:00:00')"
                    )
                    return True
                except IntegrityError:
                    return False

        assert asyncio.run(accepts("successful")) is True
        assert asyncio.run(accepts("pending")) is False
    finally:
        asyncio.run(engine.dispose())


def test_upgrade_aborts_when_the_old_vocabulary_is_still_referenced(tmp_path) -> None:
    """A DEFAULT 'pending' would break every later insert: refuse, change nothing."""
    engine = make_engine(tmp_path, "default-pending.db")

    async def setup() -> None:
        await _build_with(
            engine,
            (
                DEFAULT_PENDING_LEGACY,
                "INSERT INTO transactions VALUES (1, 1, 1, 5000, 'completed', '*150*1#',"
                " 'TXN-1', '2026-01-02 10:00:00')",
            ),
        )

    asyncio.run(setup())
    try:
        before = asyncio.run(_read_snapshot(engine))
        with pytest.raises(SchemaUpgradeError, match="pending/completed"):
            asyncio.run(migrate_transaction_lifecycle(engine))
        assert asyncio.run(_read_snapshot(engine)) == before
    finally:
        asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# Slice 2.3b: a committed sale needs no customer record
# ---------------------------------------------------------------------------

#: A database written by the 2.3/2.3a build: lifecycle columns present,
#: customer_id still NOT NULL and no transaction-level phone.
PRE_2_3B_SQL = (
    """
    CREATE TABLE transactions (
        id INTEGER NOT NULL,
        customer_id INTEGER NOT NULL,
        service_id INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        status VARCHAR(20) NOT NULL,
        ussd_string VARCHAR(255) NOT NULL,
        reference VARCHAR(255) NOT NULL,
        created_at DATETIME NOT NULL,
        idempotency_key VARCHAR(64),
        deleted_at DATETIME,
        legacy_status VARCHAR(20),
        PRIMARY KEY (id),
        CONSTRAINT transaction_status CHECK (status IN ('successful', 'failed')),
        UNIQUE (reference),
        FOREIGN KEY(customer_id) REFERENCES customers (id),
        FOREIGN KEY(service_id) REFERENCES services (id),
        UNIQUE (idempotency_key)
    )
    """,
    "INSERT INTO transactions (id, customer_id, service_id, amount_cents, status,"
    " ussd_string, reference, created_at)"
    " VALUES (1, 1, 1, 5000, 'successful', '*150*1#', 'TXN-linked', '2026-01-02 10:00:00')",
    "INSERT INTO transactions (id, customer_id, service_id, amount_cents, status,"
    " ussd_string, reference, created_at, legacy_status)"
    " VALUES (2, 1, 1, 4000, 'failed', '*150*2#', 'TXN-imported', '2026-01-03 11:00:00', 'pending')",
    "INSERT INTO profit_ledger (transaction_id, operator_code, amount_cents,"
    " commission_cents, profit_cents, created_at)"
    " VALUES (1, 'mtn', 5000, 250, 250, '2026-01-02 10:00:00')",
)


def build_pre_2_3b(tmp_path):
    engine = make_engine(tmp_path, "pre-2-3b.db")
    asyncio.run(_build_with(engine, PRE_2_3B_SQL))
    return engine


#: Columns the 2.3b snapshot compares; ``customer_phone`` may not exist yet.
SNAPSHOT_COLUMNS = (
    "id",
    "customer_id",
    "service_id",
    "amount_cents",
    "status",
    "reference",
    "ussd_string",
    "created_at",
    "idempotency_key",
    "deleted_at",
    "legacy_status",
    "customer_phone",
)


async def _customer_snapshot(engine) -> dict:
    async with engine.connect() as connection:
        present = {
            row[1]
            for row in (
                await connection.exec_driver_sql("PRAGMA table_info(transactions)")
            ).all()
        }
        selected = ", ".join(
            name for name in SNAPSHOT_COLUMNS if name in present
        )
        return {
            "rows": [
                tuple(row)
                for row in (
                    await connection.exec_driver_sql(
                        f"SELECT {selected} FROM transactions ORDER BY id"
                    )
                ).all()
            ],
            "has_customer_phone": "customer_phone" in present,
            "customers": [
                tuple(row)
                for row in (
                    await connection.exec_driver_sql("SELECT * FROM customers ORDER BY id")
                ).all()
            ],
            "ledger": [
                tuple(row)
                for row in (
                    await connection.exec_driver_sql("SELECT * FROM profit_ledger ORDER BY id")
                ).all()
            ],
            "balances": [
                tuple(row)
                for row in (
                    await connection.exec_driver_sql("SELECT * FROM operator_balance ORDER BY id")
                ).all()
            ],
            "foreign_key_check": [
                tuple(row) for row in (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
            ],
            "integrity": [
                tuple(row) for row in (await connection.exec_driver_sql("PRAGMA integrity_check")).all()
            ],
            "columns": [
                tuple(row)
                for row in (await connection.exec_driver_sql("PRAGMA table_info(transactions)")).all()
            ],
            "objects": [
                tuple(row)
                for row in (
                    await connection.exec_driver_sql(
                        "SELECT type, name, tbl_name, sql FROM sqlite_master"
                        " WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                    )
                ).all()
            ],
        }


def test_widening_preserves_every_row_customer_and_ledger_entry(tmp_path) -> None:
    engine = build_pre_2_3b(tmp_path)
    try:
        before = asyncio.run(_customer_snapshot(engine))
        assert before["has_customer_phone"] is False
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
        after = asyncio.run(_customer_snapshot(engine))
        assert after["has_customer_phone"] is True

        # Every row, id, link, amount, reference and provenance is unchanged;
        # the new phone column is backfilled from the linked customer.
        assert after["rows"] == [
            (1, 1, 1, 5000, "successful", "TXN-linked", "*150*1#",
             "2026-01-02 10:00:00", None, None, None, "0944123456"),
            (2, 1, 1, 4000, "failed", "TXN-imported", "*150*2#",
             "2026-01-03 11:00:00", None, None, "pending", "0944123456"),
        ]
        assert after["customers"] == before["customers"]
        assert after["ledger"] == before["ledger"]
        assert after["balances"] == before["balances"]
        assert after["foreign_key_check"] == before["foreign_key_check"] == []
        assert after["integrity"] == [("ok",)]

        # Only the transactions table changed: nothing else in the schema did.
        changed = {
            (kind, name, table)
            for kind, name, table, sql in after["objects"]
            if (kind, name, table, sql) not in before["objects"]
        }
        assert all(name == "transactions" for _kind, name, _table in changed)

        # The link may now be NULL, and the column order matches a fresh build.
        columns = {row[1]: row for row in after["columns"]}
        assert columns["customer_id"][3] == 0  # notnull
        assert "customer_phone" in columns
    finally:
        asyncio.run(engine.dispose())


def test_widened_schema_matches_a_freshly_created_database(tmp_path) -> None:
    engine = build_pre_2_3b(tmp_path)
    fresh = make_engine(tmp_path, "fresh-2-3b.db")

    async def setup_fresh() -> None:
        async with fresh.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(setup_fresh())
    try:
        asyncio.run(migrate_transaction_lifecycle(engine))
        migrated = asyncio.run(_read_snapshot(engine))
        created = asyncio.run(_read_snapshot(fresh))
    finally:
        asyncio.run(engine.dispose())
        asyncio.run(fresh.dispose())

    assert migrated["columns"] == created["columns"]
    assert migrated["foreign_keys"] == created["foreign_keys"]


def test_widening_is_idempotent(tmp_path) -> None:
    engine = build_pre_2_3b(tmp_path)
    try:
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
        state = asyncio.run(_customer_snapshot(engine))
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is False
        assert asyncio.run(_customer_snapshot(engine)) == state
    finally:
        asyncio.run(engine.dispose())


def test_the_widened_table_accepts_a_sale_without_a_customer(tmp_path) -> None:
    engine = build_pre_2_3b(tmp_path)
    try:
        asyncio.run(migrate_transaction_lifecycle(engine))

        async def insert() -> None:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "INSERT INTO transactions (customer_id, service_id, amount_cents, status,"
                    " ussd_string, reference, created_at, idempotency_key, customer_phone)"
                    " VALUES (NULL, 1, 1200, 'successful', '*150*12#', 'TXN-one-time',"
                    " '2026-02-01 00:00:00', 'onetimekey000000000001', '0955123456')"
                )

        asyncio.run(insert())
        assert asyncio.run(_customer_snapshot(engine))["foreign_key_check"] == []
    finally:
        asyncio.run(engine.dispose())


def test_widening_aborts_while_the_customer_link_is_still_required(tmp_path) -> None:
    """A half-widened table (phone column, link still NOT NULL) fails closed."""
    engine = make_engine(tmp_path, "half-widened.db")
    ddl = PRE_2_3B_SQL[0].replace(
        "        legacy_status VARCHAR(20),",
        "        legacy_status VARCHAR(20),\n        customer_phone VARCHAR(32),",
    )
    asyncio.run(
        _build_with(
            engine,
            (ddl, PRE_2_3B_SQL[1]),
        )
    )
    try:
        before = asyncio.run(_customer_snapshot(engine))
        with pytest.raises(SchemaUpgradeError, match="NOT NULL"):
            asyncio.run(migrate_transaction_lifecycle(engine))
        assert asyncio.run(_customer_snapshot(engine)) == before
    finally:
        asyncio.run(engine.dispose())


def test_widening_aborts_on_a_leftover_temporary_table(tmp_path) -> None:
    from backend.migrations import CUSTOMER_TABLE

    engine = build_pre_2_3b(tmp_path)

    async def add_leftover() -> None:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE TABLE {CUSTOMER_TABLE} (id INTEGER PRIMARY KEY)"
            )

    asyncio.run(add_leftover())
    try:
        before = asyncio.run(_customer_snapshot(engine))
        with pytest.raises(SchemaUpgradeError, match="leftover"):
            asyncio.run(migrate_transaction_lifecycle(engine))
        assert asyncio.run(_customer_snapshot(engine)) == before
    finally:
        asyncio.run(engine.dispose())


def test_a_legacy_placeholder_name_is_preserved_through_the_widening(tmp_path) -> None:
    """A customer whose name is its phone (v1.0.x data) is kept verbatim."""
    engine = build_pre_2_3b(tmp_path)

    async def rename_to_phone() -> None:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE customers SET name = '0944123456'")

    asyncio.run(rename_to_phone())
    try:
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
        state = asyncio.run(_customer_snapshot(engine))
        assert state["customers"][0][1:3] == ("0944123456", "0944123456")
        # The row keeps its link; only the phone column is filled in.
        assert [row[1] for row in state["rows"]] == [1, 1]
        assert state["foreign_key_check"] == []
    finally:
        asyncio.run(engine.dispose())


def test_widening_fails_closed_and_leaves_the_file_untouched(tmp_path, monkeypatch) -> None:
    from backend import migrations

    engine = build_pre_2_3b(tmp_path)
    try:
        before = asyncio.run(_customer_snapshot(engine))

        def explode(connection, plan):
            raise RuntimeError("injected failure after the widened copy")

        monkeypatch.setattr(migrations, "_verify_customer_copy", explode)
        with pytest.raises(RuntimeError, match="injected failure"):
            asyncio.run(migrate_transaction_lifecycle(engine))
        monkeypatch.undo()

        assert asyncio.run(_customer_snapshot(engine)) == before
        # Still upgradeable afterwards.
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
    finally:
        asyncio.run(engine.dispose())


def test_both_steps_apply_to_a_v1_0_x_database_in_one_run(tmp_path) -> None:
    """The 2.3 lifecycle rebuild and the 2.3b widening compose."""
    engine = build_legacy(tmp_path)
    try:
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is True
        state = asyncio.run(_customer_snapshot(engine))
        assert [row[4] for row in state["rows"]] == ["failed", "successful", "failed"]
        assert [row[11] for row in state["rows"]] == ["0944123456"] * 3
        assert state["foreign_key_check"] == [] and state["integrity"] == [("ok",)]
        assert asyncio.run(migrate_transaction_lifecycle(engine)) is False
    finally:
        asyncio.run(engine.dispose())
