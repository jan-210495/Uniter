"""Guarded, data-preserving schema upgrades for existing Uniter SQLite databases.

This revision changes the meaning of a persisted transaction: a row now exists
only once the seller committed the sale, so the status vocabulary is exactly
``successful``/``failed``. v1.0.0/v1.0.1/v1.0.3 databases store
``pending``/``completed``/``failed`` and enforce that with a CHECK constraint,
and SQLite cannot alter a CHECK constraint in place.

The upgrade therefore rebuilds the ``transactions`` table with the official
copy procedure, but it does **not** hand-write the new schema: it reads the live
``CREATE TABLE`` statement from ``sqlite_master``, parses it, and rewrites only
what must change (the table name, the status CHECK list, and three added
columns). Every other column, DEFAULT, NOT NULL, UNIQUE, CHECK, FOREIGN KEY and
table option (e.g. ``WITHOUT ROWID``) is carried over verbatim, and every
explicit index and trigger attached to the table is recreated from its stored
SQL. Objects that merely reference ``transactions`` (views, triggers on other
tables, foreign keys from other tables) are never touched and are verified
unchanged afterwards.

Properties of the upgrade:

* **Atomic** — every statement runs inside one ``BEGIN IMMEDIATE`` transaction.
  Any failure rolls back and leaves the original table, its indexes, its
  triggers and all rows exactly as they were. A raise from the caller's startup
  path therefore fails closed instead of serving an incompatible schema.
* **Idempotent** — the live schema is inspected first (and again inside the
  write lock); a current database is left alone and ``False`` is returned.
* **Concurrency-safe** — ``BEGIN IMMEDIATE`` takes SQLite's write lock, so two
  application starts cannot rebuild the table at the same time; the loser waits
  (``busy_timeout``) and then finds the schema already upgraded.
* **Auditable** — the legacy status of every imported row is preserved in
  ``legacy_status`` (``pending``/``completed``/``failed``/``successful``), so the
  mapping is traceable and reversible. No accounting row is touched: the
  operator balances, ``profit_ledger`` rows, commission and customer records are
  copied by reference only.

Status mapping (operational meaning, not just schema):

* ``completed`` -> ``successful`` — the operator confirmed the sale.
* ``pending`` -> ``failed`` — a code was generated but no Copy/dialer commit
  happened, so it was never a committed sale. Its operator debit and
  ``profit_ledger`` row are **kept unchanged** (no automatic refund/reversal);
  ``legacy_status = 'pending'`` marks the row, and it stays removable through the
  failed-row action.
* ``failed``/``successful`` rows keep their status.

Rolling back a database by hand (if a build must be abandoned) is the same
mapping in reverse, using ``legacy_status``:

    completed  <- 'successful' where legacy_status IN ('completed', NULL, 'successful')
    pending    <- 'failed'     where legacy_status = 'pending'
    failed     <- 'failed'     where legacy_status IN ('failed', NULL)

Unsafe or unrecognised schemas (unknown status values, unexpected columns, an
unparseable status CHECK) abort before the original table is replaced.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

#: The only statuses a persisted transaction can carry after the upgrade.
FINAL_STATUSES = ("successful", "failed")

#: Statuses written by a previous release, plus the current ones (a database
#: created by this build and then downgraded by hand).
LEGACY_STATUSES = ("pending", "completed", "failed", "successful")

#: Explicit, documented mapping of every accepted legacy status.
STATUS_MAP = {
    "completed": "successful",
    "pending": "failed",
    "failed": "failed",
    "successful": "successful",
}

#: The mapping as SQL, generated from ``STATUS_MAP`` so the copy and the
#: documentation can never drift apart. No ELSE: an unmapped value aborts the
#: copy (NOT NULL) instead of being guessed at.
_STATUS_CASE_SQL = (
    "CASE status "
    + " ".join(f"WHEN '{legacy}' THEN '{final}'" for legacy, final in STATUS_MAP.items())
    + " END"
)

#: Columns this revision adds to ``transactions`` (name, SQLite type).
ADDED_COLUMNS = (
    ("idempotency_key", "VARCHAR(64)"),
    ("deleted_at", "DATETIME"),
    ("legacy_status", "VARCHAR(20)"),
)

#: Columns the application needs on every version of the table.
REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "customer_id",
        "service_id",
        "amount_cents",
        "status",
        "ussd_string",
        "reference",
        "created_at",
    }
)

TABLE = "transactions"
LEGACY_TABLE = "transactions_legacy"
STATUS_CONSTRAINT = "transaction_status"

_CREATE_TABLE_RE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?P<name>"[^"]+"|`[^`]+`|\[[^\]]+\]|[^\s(]+)\s*\(',
    re.IGNORECASE,
)
_STATUS_CHECK_RE = re.compile(
    r"(?P<prefix>\bCHECK\s*\(\s*\"?status\"?\s+IN\s*\()(?P<values>[^)]*)(?P<suffix>\)\s*\))",
    re.IGNORECASE,
)
_CONSTRAINT_KEYWORDS = ("CONSTRAINT", "PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY")

#: Pragmas used around the rebuild. ``legacy_alter_table`` keeps a rename from
#: rewriting other objects' SQL (views, triggers and other tables' foreign keys
#: all name ``transactions``); without it SQLite >= 3.25 would follow the rename
#: and leave those objects pointing at the temporary table.
PRAGMA_LEGACY_ALTER_ON = "PRAGMA legacy_alter_table=ON"
PRAGMA_LEGACY_ALTER_OFF = "PRAGMA legacy_alter_table=OFF"


class SchemaUpgradeError(RuntimeError):
    """The database cannot be upgraded safely, so nothing was changed.

    Callers (``init_db``, the desktop seed and the application startup hook)
    let this propagate: the process fails closed instead of serving a schema it
    does not understand. The database file is left exactly as it was found.
    """


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Object:
    """One ``sqlite_master`` row (tables, indexes, triggers, views)."""

    type: str
    name: str
    tbl_name: str
    sql: str | None


@dataclass(frozen=True)
class _Column:
    name: str
    type: str
    notnull: bool
    default: str | None
    primary_key: bool


@dataclass(frozen=True)
class _Plan:
    """Everything the rebuild needs, captured before the table is touched."""

    ddl: str
    new_ddl: str
    columns: tuple[str, ...]
    indexes: tuple[_Object, ...]
    triggers: tuple[_Object, ...]
    other_objects: tuple[_Object, ...]
    foreign_key_violations: frozenset[tuple]
    row_count: int


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _unquote(identifier: str) -> str:
    text = identifier.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in '"`[]':
        inner = text[1:-1]
        return inner.replace('""', '"') if text[0] == '"' else inner
    return text


def _master_objects(connection) -> list[_Object]:
    rows = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).all()
    return [_Object(str(kind), str(name), str(table), sql) for kind, name, table, sql in rows]


def _table_columns(connection, table: str) -> tuple[_Column, ...]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({_quote(table)})").all()
    return tuple(
        _Column(str(name), str(col_type or ""), bool(notnull), default, bool(pk))
        for _cid, name, col_type, notnull, default, pk in rows
    )


def _has_table(connection, table: str) -> bool:
    return bool(
        connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).first()
    )


def _foreign_key_violations(connection) -> frozenset[tuple]:
    rows = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    return frozenset(tuple(row) for row in rows)


# ---------------------------------------------------------------------------
# Parsing the live CREATE TABLE statement
# ---------------------------------------------------------------------------


def _split_create_table(ddl: str) -> tuple[str, list[str], str]:
    """Split ``CREATE TABLE ... (defs) [trailer]`` into body definitions.

    Returns ``(name, definitions, trailer)`` where ``trailer`` keeps anything
    after the closing parenthesis (``WITHOUT ROWID``, ``STRICT``, ...).
    """
    match = _CREATE_TABLE_RE.match(ddl)
    if match is None:
        raise SchemaUpgradeError("transactions table has an unrecognised CREATE TABLE statement")
    name = _unquote(match.group("name"))
    body_start = match.end()
    depth = 1
    quote: str | None = None
    index = body_start
    while index < len(ddl):
        char = ddl[index]
        if quote is not None:
            if char == quote:
                # Doubled quotes are escapes inside string/identifier literals.
                if index + 1 < len(ddl) and ddl[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in ("'", '"', "`"):
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                body = ddl[body_start:index]
                trailer = ddl[index + 1 :]
                return name, _split_definitions(body), trailer
        index += 1
    raise SchemaUpgradeError("transactions table has an unbalanced CREATE TABLE statement")


def _split_definitions(body: str) -> list[str]:
    definitions: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    index = 0
    while index < len(body):
        char = body[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(body) and body[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in ("'", '"', "`"):
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            definitions.append(body[start:index].strip())
            start = index + 1
        index += 1
    tail = body[start:].strip()
    if tail:
        definitions.append(tail)
    return [definition for definition in definitions if definition]


def _is_table_constraint(definition: str) -> bool:
    upper = definition.upper()
    return any(upper.startswith(keyword) for keyword in _CONSTRAINT_KEYWORDS)


def _definition_name(definition: str) -> str:
    """Return the column or constraint name of a definition."""
    text = definition.strip()
    if text.upper().startswith("CONSTRAINT "):
        parts = text.split(None, 2)
        return _unquote(parts[1]) if len(parts) > 1 else ""
    if _is_table_constraint(text):
        return ""
    first = text.split(None, 1)[0]
    return _unquote(first)


def _rewrite_status_checks(definitions: list[str]) -> int:
    """Rewrite every status CHECK in place; return how many were found.

    A database may declare the vocabulary once as a table constraint, inline on
    the column, or (after a hand edit) both. Every one of them must accept
    exactly the current statuses, otherwise the rebuilt table would reject a
    legitimate write or the upgrade would silently keep a dead constraint.
    """
    rewritten = 0
    for index, definition in enumerate(definitions):
        upper = definition.upper()
        if "CHECK" not in upper or "STATUS" not in upper:
            continue
        match = _STATUS_CHECK_RE.search(definition)
        if match is None:
            raise SchemaUpgradeError(
                "transactions.status CHECK constraint is not a simple IN (...) list"
            )
        values = [
            value.replace("''", "'") for value in re.findall(r"'((?:[^']|'')*)'", match.group("values"))
        ]
        unknown = sorted(set(values) - set(LEGACY_STATUSES) - set(FINAL_STATUSES))
        if unknown:
            raise SchemaUpgradeError(
                "transactions.status CHECK allows unexpected values: " + ", ".join(unknown)
            )
        if set(values) != set(FINAL_STATUSES):
            definitions[index] = _STATUS_CHECK_RE.sub(
                lambda found: found.group("prefix") + "'successful', 'failed'" + found.group("suffix"),
                definition,
                count=1,
            )
        rewritten += 1
    return rewritten


def _rewrite_ddl(ddl: str) -> tuple[str, tuple[str, ...]]:
    """Return the new CREATE TABLE statement for the rebuilt ``transactions``.

    Only the table name, the status CHECK list and the three added columns are
    changed; every other definition is copied verbatim.
    """
    name, definitions, trailer = _split_create_table(ddl)
    if name != TABLE:
        raise SchemaUpgradeError(f"unexpected table name in transactions DDL: {name!r}")
    if not definitions:
        raise SchemaUpgradeError("transactions table DDL has no column definitions")

    if _rewrite_status_checks(definitions) == 0:
        # A database created without the historical CHECK still upgrades: the
        # constraint is added so the new vocabulary is enforced from now on.
        definitions.append(
            f"CONSTRAINT {STATUS_CONSTRAINT} CHECK (status IN ('successful', 'failed'))"
        )

    existing_columns = {
        _definition_name(definition)
        for definition in definitions
        if not _is_table_constraint(definition)
    }
    inserted = [
        f"{column_name} {column_type}"
        for column_name, column_type in ADDED_COLUMNS
        if column_name not in existing_columns
    ]
    if inserted:
        # Columns stay together with the other columns, before table constraints,
        # so the rebuilt table matches a freshly created one.
        first_constraint = next(
            (index for index, definition in enumerate(definitions) if _is_table_constraint(definition)),
            len(definitions),
        )
        definitions[first_constraint:first_constraint] = inserted

    if not _has_unique_on(definitions, "idempotency_key"):
        definitions.append("UNIQUE (idempotency_key)")

    body = ",\n    ".join(definitions)
    if "'pending'" in body or "'completed'" in body:
        # A DEFAULT or a second predicate still referring to the retired
        # vocabulary would make the rebuilt table reject real writes.
        raise SchemaUpgradeError(
            "transactions table still refers to the pending/completed vocabulary"
        )
    new_ddl = f"CREATE TABLE {_quote(TABLE)} (\n    {body}\n){trailer}"
    return new_ddl, _definition_column_names(definitions)


def _has_unique_on(definitions: list[str], column: str) -> bool:
    """True when a UNIQUE constraint/index on ``column`` is already declared."""
    for definition in definitions:
        upper = definition.upper()
        if "UNIQUE" not in upper:
            continue
        if re.search(rf"UNIQUE\s*\(\s*\"?{re.escape(column)}\"?\s*\)", definition, re.IGNORECASE):
            return True
        if re.search(rf"\"?{re.escape(column)}\"?[^,]*\bUNIQUE\b", definition, re.IGNORECASE):
            return True
    return False


def _definition_column_names(definitions: list[str]) -> tuple[str, ...]:
    """Column names, in declaration order (table constraints excluded)."""
    return tuple(
        _definition_name(definition)
        for definition in definitions
        if not _is_table_constraint(definition)
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _build_plan(connection) -> _Plan | None:
    """Inspect the live table and produce a rebuild plan, or ``None`` if current.

    Raises ``SchemaUpgradeError`` for any schema that cannot be upgraded safely;
    nothing is written at this stage.
    """
    if not _has_table(connection, TABLE):
        return None

    ddl = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).scalar()
    if not ddl:
        raise SchemaUpgradeError("transactions table has no recorded CREATE TABLE statement")

    columns = _table_columns(connection, TABLE)
    names = {column.name for column in columns}
    missing = sorted(REQUIRED_COLUMNS - names)
    if missing:
        raise SchemaUpgradeError(
            "transactions table is missing required columns: " + ", ".join(missing)
        )

    present_new = [name for name, _ in ADDED_COLUMNS if name in names]
    if len(present_new) == len(ADDED_COLUMNS):
        # Already upgraded. Double-check the vocabulary so a hand-edited table
        # cannot be served silently.
        statuses, missing = _status_values(connection)
        if missing:
            raise SchemaUpgradeError("transactions table has rows without a status")
        unknown = sorted(statuses - set(FINAL_STATUSES))
        if unknown:
            raise SchemaUpgradeError(
                "transactions table has unexpected status values: " + ", ".join(unknown)
            )
        return None
    if present_new:
        raise SchemaUpgradeError(
            "transactions table has a partially upgraded column set: " + ", ".join(present_new)
        )

    statuses, missing = _status_values(connection)
    if missing:
        raise SchemaUpgradeError("transactions table has rows without a status")
    unknown = sorted(statuses - set(LEGACY_STATUSES) - set(FINAL_STATUSES))
    if unknown:
        raise SchemaUpgradeError(
            "transactions table has status values this upgrade does not understand: "
            + ", ".join(unknown)
        )

    for column in columns:
        if (
            column.name not in REQUIRED_COLUMNS
            and column.notnull
            and column.default is None
            and not column.primary_key
        ):
            raise SchemaUpgradeError(
                "transactions table has an unknown NOT NULL column without a default: "
                + column.name
            )

    if _has_table(connection, LEGACY_TABLE):
        raise SchemaUpgradeError(
            f"a leftover {LEGACY_TABLE!r} table exists; refusing to overwrite it"
        )

    new_ddl, new_columns = _rewrite_ddl(ddl)
    objects = _master_objects(connection)
    indexes = tuple(
        obj for obj in objects if obj.type == "index" and obj.tbl_name == TABLE and obj.sql
    )
    triggers = tuple(obj for obj in objects if obj.type == "trigger" and obj.tbl_name == TABLE)
    other_objects = tuple(
        obj
        for obj in objects
        if not (obj.type == "table" and obj.name == TABLE)
        and not (obj.type in {"index", "trigger"} and obj.tbl_name == TABLE)
    )
    row_count = int(
        connection.exec_driver_sql(f"SELECT COUNT(*) FROM {_quote(TABLE)}").scalar() or 0
    )
    return _Plan(
        ddl=ddl,
        new_ddl=new_ddl,
        columns=new_columns,
        indexes=indexes,
        triggers=triggers,
        other_objects=other_objects,
        foreign_key_violations=_foreign_key_violations(connection),
        row_count=row_count,
    )


def _status_values(connection) -> tuple[set[str], int]:
    """Return the distinct status values and how many rows have no status."""
    rows = connection.exec_driver_sql(
        f"SELECT status, COUNT(*) FROM {_quote(TABLE)} GROUP BY status"
    ).all()
    values = {str(value) for value, _count in rows if value is not None}
    missing = sum(int(count) for value, count in rows if value is None)
    return values, missing


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _source_columns(plan: _Plan) -> list[str]:
    """Columns of the legacy table, in their original order."""
    return list(_definition_column_names(_split_create_table(plan.ddl)[1]))


def _copy_statement(plan: _Plan) -> str:
    """Copy every legacy column, mapping status and recording its legacy value.

    Unknown extra columns are carried over verbatim, so nothing is dropped; the
    three new columns start empty except ``legacy_status`` (provenance).
    """
    source = _source_columns(plan)
    expressions = [
        _STATUS_CASE_SQL if name == "status" else _quote(name) for name in source
    ]
    targets = ", ".join(_quote(name) for name in source)
    target_sql = targets + ", idempotency_key, deleted_at, legacy_status"
    expression_sql = ", ".join(expressions) + ", NULL, NULL, status"
    return (
        f"INSERT INTO {_quote(TABLE)} ({target_sql}) "
        f"SELECT {expression_sql} FROM {_quote(LEGACY_TABLE)} ORDER BY id"
    )


def _apply(connection, plan: _Plan) -> None:
    """Rebuild the table inside the caller's transaction."""
    connection.exec_driver_sql(f"ALTER TABLE {_quote(TABLE)} RENAME TO {_quote(LEGACY_TABLE)}")
    connection.exec_driver_sql(plan.new_ddl)
    connection.exec_driver_sql(_copy_statement(plan))
    _verify_copy(connection, plan)
    connection.exec_driver_sql(f"DROP TABLE {_quote(LEGACY_TABLE)}")
    for index in plan.indexes:
        connection.exec_driver_sql(index.sql or "")
    for trigger in plan.triggers:
        connection.exec_driver_sql(trigger.sql or "")
    _verify_rebuild(connection, plan)


def _verify_copy(connection, plan: _Plan) -> None:
    copied = int(connection.exec_driver_sql(f"SELECT COUNT(*) FROM {_quote(TABLE)}").scalar() or 0)
    if copied != plan.row_count:
        raise SchemaUpgradeError(
            f"row copy mismatch: expected {plan.row_count}, copied {copied}"
        )
    missing = connection.exec_driver_sql(
        f"SELECT COUNT(*) FROM (SELECT id FROM {_quote(LEGACY_TABLE)} "
        f"EXCEPT SELECT id FROM {_quote(TABLE)})"
    ).scalar()
    extra = connection.exec_driver_sql(
        f"SELECT COUNT(*) FROM (SELECT id FROM {_quote(TABLE)} "
        f"EXCEPT SELECT id FROM {_quote(LEGACY_TABLE)})"
    ).scalar()
    if missing or extra:
        raise SchemaUpgradeError("row identifiers changed while copying transactions")
    unexpected = connection.exec_driver_sql(
        f"SELECT COUNT(*) FROM {_quote(TABLE)} WHERE status NOT IN ('successful', 'failed')"
    ).scalar()
    if unexpected:
        raise SchemaUpgradeError("status mapping left rows outside successful/failed")
    untraced = connection.exec_driver_sql(
        f"SELECT COUNT(*) FROM {_quote(TABLE)} WHERE legacy_status IS NULL"
    ).scalar()
    if untraced:
        raise SchemaUpgradeError("status mapping did not record the legacy status")


def _verify_rebuild(connection, plan: _Plan) -> None:
    expected = list(plan.columns)
    actual = [column.name for column in _table_columns(connection, TABLE)]
    if actual != expected:
        raise SchemaUpgradeError(
            f"rebuilt transactions columns differ: {actual} != {expected}"
        )

    objects = _master_objects(connection)
    untouched = {
        (obj.type, obj.name, obj.tbl_name, obj.sql)
        for obj in objects
        if not (obj.type == "table" and obj.name == TABLE)
        and not (obj.type in {"index", "trigger"} and obj.tbl_name == TABLE)
    }
    expected_untouched = {
        (obj.type, obj.name, obj.tbl_name, obj.sql) for obj in plan.other_objects
    }
    if untouched != expected_untouched:
        changed = sorted(expected_untouched.symmetric_difference(untouched))
        raise SchemaUpgradeError(
            "other schema objects changed during the upgrade: "
            + "; ".join(str(entry[1]) for entry in changed)
        )

    recreated = {
        (obj.type, obj.name, obj.tbl_name, obj.sql)
        for obj in objects
        if obj.type in {"index", "trigger"} and obj.tbl_name == TABLE
    }
    expected_recreated = {
        (obj.type, obj.name, obj.tbl_name, obj.sql) for obj in (*plan.indexes, *plan.triggers)
    }
    if recreated != expected_recreated:
        raise SchemaUpgradeError("indexes or triggers were not restored identically")

    violations = _foreign_key_violations(connection)
    introduced = violations - plan.foreign_key_violations
    if introduced:
        raise SchemaUpgradeError(
            f"foreign key violations after the upgrade: {len(introduced)}"
        )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _run(connection) -> bool:
    plan = _build_plan(connection)
    if plan is None:
        return False
    _apply(connection, plan)
    return True


async def migrate_transaction_lifecycle(engine: AsyncEngine) -> bool:
    """Upgrade the transactions table in place; return True when it changed.

    Safe to call on a current or empty database (no-op) and safe to call
    repeatedly or from two processes at once. Raises ``SchemaUpgradeError``
    (leaving the database untouched) when the schema cannot be upgraded safely.
    """
    async with engine.connect() as connection:
        # Maintenance work needs explicit transaction control, and PRAGMA
        # statements only take effect outside an implicit transaction.
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        precheck = await connection.run_sync(_build_plan)
        if precheck is None:
            return False

        await connection.exec_driver_sql("PRAGMA busy_timeout=5000")
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        # Renames must not rewrite other objects' SQL (views, triggers, or the
        # foreign keys of other tables that reference transactions).
        await connection.exec_driver_sql(PRAGMA_LEGACY_ALTER_ON)
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            # Re-inspect inside the write lock: another start may have won.
            changed = await connection.run_sync(_run)
            await connection.exec_driver_sql("COMMIT")
        except Exception:
            await connection.exec_driver_sql("ROLLBACK")
            logger.error("Transactions schema upgrade failed; the database was left unchanged")
            raise
        finally:
            await connection.exec_driver_sql(PRAGMA_LEGACY_ALTER_OFF)
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    if changed:
        logger.info("Upgraded transactions to the successful/failed lifecycle")
    return changed


async def upgrade_existing_database(engine: AsyncEngine, database_path: str | None) -> bool:
    """Startup policy: upgrade an already existing database file, or explain why not.

    * missing file -> ``False``: a fresh install is created (and upgraded) by the
      seeding entry points; nothing is created implicitly here.
    * empty file (no application tables) -> ``False``: uninitialised database,
      the same state previous releases served; nothing to migrate.
    * partially initialised schema (application tables but no ``transactions``)
      -> ``SchemaUpgradeError``: fail closed instead of serving a broken schema.
    * existing v1.0.x database -> guarded rebuild as described above.
    """
    if not database_path or not os.path.exists(database_path):
        return False
    async with engine.connect() as connection:
        result = await connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = {str(row[0]) for row in result.all()}
    if TABLE not in tables:
        if tables:
            raise SchemaUpgradeError(
                "the database has an incomplete schema (no transactions table); "
                "run the seeding entry point to initialise it"
            )
        logger.warning(
            "Database file %s exists but has no tables; run the seeding entry point",
            database_path,
        )
        return False
    return await migrate_transaction_lifecycle(engine)


__all__ = [
    "ADDED_COLUMNS",
    "FINAL_STATUSES",
    "LEGACY_STATUSES",
    "STATUS_MAP",
    "SchemaUpgradeError",
    "migrate_transaction_lifecycle",
    "upgrade_existing_database",
]
