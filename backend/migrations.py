"""Guarded schema upgrades for existing Uniter SQLite databases.

Only the transaction lifecycle needs migrating in this revision: v1.0.1/v1.0.3
stored transactions as ``pending``/``completed``/``failed``, while a persisted
row now means "the seller committed this sale" and therefore carries exactly
``successful`` or ``failed``. SQLite cannot alter a CHECK constraint in place,
so the table is rebuilt with the official copy-and-rename procedure, keeping
every row, reference, amount and the related ``profit_ledger`` entries.

The migration is idempotent: it inspects the live schema first and does nothing
when the database is already current (for example a freshly created one).
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

#: Columns added by this revision to the transactions table.
ADDED_COLUMNS = ("idempotency_key", "deleted_at", "legacy_status")

_REBUILT_TABLE = """
CREATE TABLE transactions_rebuilt (
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
    UNIQUE (idempotency_key),
    FOREIGN KEY(customer_id) REFERENCES customers (id),
    FOREIGN KEY(service_id) REFERENCES services (id)
)
"""

# Legacy ``completed`` = the operator confirmed the sale; legacy ``pending`` was
# never a confirmed sale, so it enters the new lifecycle as ``failed`` (and can
# be removed). No money moves either way; the original value is kept in
# ``legacy_status`` so nothing is lost.
_COPY_ROWS = """
INSERT INTO transactions_rebuilt (
    id, customer_id, service_id, amount_cents, status, ussd_string, reference,
    created_at, idempotency_key, deleted_at, legacy_status
)
SELECT
    id, customer_id, service_id, amount_cents,
    CASE status
        WHEN 'completed' THEN 'successful'
        WHEN 'pending' THEN 'failed'
        ELSE status
    END,
    ussd_string, reference, created_at, NULL, NULL,
    CASE WHEN status IN ('completed', 'pending') THEN status ELSE NULL END
FROM transactions
"""

_ADDED_COLUMN_DDL = {
    "idempotency_key": "ALTER TABLE transactions ADD COLUMN idempotency_key VARCHAR(64)",
    "deleted_at": "ALTER TABLE transactions ADD COLUMN deleted_at DATETIME",
    "legacy_status": "ALTER TABLE transactions ADD COLUMN legacy_status VARCHAR(20)",
}


def _inspect_transactions(connection) -> tuple[set[str], str | None] | None:
    """Return the transactions columns and CREATE statement, or None."""
    inspector = inspect(connection)
    if not inspector.has_table("transactions"):
        return None
    columns = {column["name"] for column in inspector.get_columns("transactions")}
    ddl = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'"
    ).scalar()
    return columns, ddl


def _needs_rebuild(ddl: str | None) -> bool:
    if not ddl:
        return False
    lowered = ddl.lower()
    return "'pending'" in lowered or "'completed'" in lowered


async def migrate_transaction_lifecycle(engine: AsyncEngine) -> bool:
    """Upgrade the transactions table in place; return True when it changed."""
    async with engine.connect() as connection:
        # PRAGMA statements and table rebuilds need to run outside an implicit
        # transaction, so this maintenance connection is fully explicit.
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        state = await connection.run_sync(_inspect_transactions)
        if state is None:
            return False
        columns, ddl = state
        rebuild = _needs_rebuild(ddl)
        missing = [name for name in ADDED_COLUMNS if name not in columns]
        if not rebuild and not missing:
            return False

        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            if rebuild:
                await connection.exec_driver_sql("DROP TABLE IF EXISTS transactions_rebuilt")
                await connection.exec_driver_sql(_REBUILT_TABLE)
                await connection.exec_driver_sql(_COPY_ROWS)
                await connection.exec_driver_sql("DROP TABLE transactions")
                await connection.exec_driver_sql(
                    "ALTER TABLE transactions_rebuilt RENAME TO transactions"
                )
            else:
                for name in missing:
                    await connection.exec_driver_sql(_ADDED_COLUMN_DDL[name])
                await connection.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_transactions_idempotency_key "
                    "ON transactions (idempotency_key)"
                )
            await connection.exec_driver_sql("COMMIT")
        except Exception:
            await connection.exec_driver_sql("ROLLBACK")
            raise
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    logger.info(
        "Migrated transactions to the successful/failed lifecycle (%s)",
        "table rebuilt" if rebuild else "columns added",
    )
    return True
