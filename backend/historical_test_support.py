"""Historical schemas used only to verify pre-v1.0.5 migration history.

Do not use these tables in current application fixtures or initialization.
The DDL comes from the checked-in v1.0.4 database dump, not current ORM models.
"""
import re
from pathlib import Path


async def install_historical_directory(connection, *, with_customer=False):
    dump = Path(__file__).with_name("fixtures").joinpath("v104_slice4.sql").read_text()
    for table in ("customers", "debt_ledger"):
        ddl = re.search(rf"CREATE TABLE {table} \(.*?\n\);", dump, re.S).group(0)
        await connection.exec_driver_sql(ddl)
    if with_customer:
        await connection.exec_driver_sql(
            "INSERT INTO customers (id, phone, name, balance_cents, created_at, updated_at) "
            "VALUES (1, '0944123456', 'Ahmad', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )


def current_transaction_columns(columns):
    """Compare unchanged column definitions, not historical directory linkage."""
    return [tuple(row)[1:] for row in columns if row[1] != "customer_id"]
