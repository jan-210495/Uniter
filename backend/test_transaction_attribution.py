"""v1.0.4 Slice 3: who performed each new transaction, and filtering by it.

``transactions.performed_by_user_id`` is a nullable foreign key to ``users.id``
with ``ON DELETE RESTRICT`` (accounts are deactivated, never deleted, and the
history must never lose its performer). New commits record the account of the
server-side session; nothing the client sends can choose it. Legacy rows keep
``NULL`` forever and are listed as "Legacy / unknown".

Upgrade scenarios only ever run against disposable databases under
``tmp_path``. The v1.0.3 fixture uses the exact ``transactions`` DDL that
v1.0.3 creates (``database.py`` only gained the auth tables since that tag),
without any auth tables, exactly as a v1.0.3 install looks on disk.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import URL, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from backend.historical_test_support import install_historical_directory, current_transaction_columns
from backend import database, seed
from backend.auth_test_support import sign_in
from backend.sim_test_support import fund_company, remember_sim
from backend.database import (
    Base,
    OperatorBalance,
    SimBalance,
    SimLedger,
    ProfitLedger,
    Transaction,
    User,
    get_db,
)
from backend.main import app
from backend.migrations import SchemaUpgradeError, migrate_transaction_lifecycle

TEMPLATES = Path(__file__).with_name("templates")
HARNESS = Path(__file__).with_name("transactions_actions_harness.cjs")
COLUMN = "performed_by_user_id"

#: ``CREATE TABLE transactions`` exactly as v1.0.3 (and v1.0.4 slices 1-2)
#: create it through ``Base.metadata.create_all``.
V103_TRANSACTIONS_DDL = """
CREATE TABLE transactions (
	id INTEGER NOT NULL,
	customer_id INTEGER,
	service_id INTEGER NOT NULL,
	amount_cents INTEGER NOT NULL,
	status VARCHAR(20) NOT NULL,
	ussd_string VARCHAR(255) NOT NULL,
	reference VARCHAR(255) NOT NULL,
	created_at DATETIME NOT NULL,
	idempotency_key VARCHAR(64),
	deleted_at DATETIME,
	legacy_status VARCHAR(20),
	customer_phone VARCHAR(32),
	PRIMARY KEY (id),
	CONSTRAINT transaction_status CHECK (status IN ('successful', 'failed')),
	FOREIGN KEY(customer_id) REFERENCES customers (id),
	FOREIGN KEY(service_id) REFERENCES services (id),
	UNIQUE (reference),
	UNIQUE (idempotency_key)
)
"""

AUTH_TABLES = {"users", "user_sessions", "login_attempts"}
#: Tables whose every row must survive the upgrade byte for byte.
PRESERVED_TABLES = (
    "customers",
    "operators",
    "services",
    "operator_balance",
    "profit_ledger",
    "debt_ledger",
    "operator_topup",
    "operator_credit_refund",
    "settings",
)
V103_COLUMNS = (
    "id, customer_id, service_id, amount_cents, status, ussd_string, reference,"
    " created_at, idempotency_key, deleted_at, legacy_status, customer_phone"
)

_keys = itertools.count(1)


def new_commit_key() -> str:
    return f"attr-commit-key-{next(_keys):08d}"


# ---------------------------------------------------------------------------
# Environment for the startup paths (same wiring as test_auth.py)
# ---------------------------------------------------------------------------


@dataclass
class Env:
    engine: AsyncEngine
    sessions: async_sessionmaker

    def sql(self, statement: str, **params):
        async def run():
            async with self.engine.connect() as connection:
                return (await connection.execute(text(statement), params)).all()
        return asyncio.run(run())


def _engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(path)))
    event.listen(engine.sync_engine, "connect", database.enable_sqlite_foreign_keys)
    return engine


@pytest.fixture
def env(tmp_path, monkeypatch) -> Iterator[Env]:
    from backend import desktop

    engine = _engine(tmp_path / "attribution.db")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "DATABASE_URL", engine.url)
    monkeypatch.setattr(seed, "SessionLocal", sessions)
    monkeypatch.setattr(desktop, "engine", engine)
    monkeypatch.setattr(desktop, "SessionLocal", sessions)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        yield Env(engine, sessions)
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


async def _build_v103(engine: AsyncEngine, sessions: async_sessionmaker) -> None:
    """A v1.0.3 install with history: linked, one-time, failed and removed rows."""
    v103_tables = [
        table for table in Base.metadata.sorted_tables
        # v1.0.3 had neither accounts nor SIM cards/balances (added in v1.0.4).
        if table.name not in AUTH_TABLES | {"transactions", "sim_cards", "sim_balances", "sim_ledger", "sim_defaults"}
    ]
    async with engine.begin() as connection:
        await install_historical_directory(connection)
        await connection.exec_driver_sql(V103_TRANSACTIONS_DDL)
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=v103_tables))
        await connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        await connection.exec_driver_sql(
            "INSERT INTO schema_migrations(version) VALUES ('slice-2.3c-operator-credit-refund')"
        )
    async with sessions() as session:
        await seed.seed_data(session)
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "INSERT INTO customers (id, phone, name, balance_cents, created_at, updated_at)"
            " VALUES (1, '0933123456', 'Ahmad', 0, '2026-01-01 08:00:00', '2026-01-01 08:00:00')"
        )
        await connection.exec_driver_sql(
            "UPDATE operator_balance SET balance_cents = 123456"
        )
        # v1.0.3's init_db stored the commission rate; a non-default value
        # proves no startup path rewrites it.
        await connection.exec_driver_sql(
            "INSERT INTO settings (key, value, updated_at)"
            " VALUES ('commission_rate', '0.07', '2026-01-01 08:00:00')"
        )
        await connection.exec_driver_sql(
            f"INSERT INTO transactions ({V103_COLUMNS}) VALUES"
            " (1, 1, 1, 5000, 'successful', '*150*1*0933123456*5000#', 'TXN-legacy-a',"
            "  '2026-01-02 10:00:00', 'legacy-key-aaaaaaaa', NULL, NULL, '0933123456'),"
            " (2, NULL, 1, 1200, 'successful', '*150*1*0955123456*1200#', 'TXN-legacy-b',"
            "  '2026-01-03 11:00:00', NULL, NULL, 'completed', '0955123456'),"
            " (3, 1, 1, 700, 'failed', '*150*1*0933123456*700#', 'TXN-legacy-c',"
            "  '2026-01-04 12:00:00', NULL, '2026-01-05 09:00:00', 'pending', '0933123456')"
        )
        await connection.exec_driver_sql(
            "INSERT INTO profit_ledger (id, transaction_id, operator_code, amount_cents,"
            " commission_cents, profit_cents, created_at) VALUES"
            " (1, 1, 'mtn', 5000, 250, 250, '2026-01-02 10:00:00'),"
            " (2, 2, 'mtn', 1200, 60, 60, '2026-01-03 11:00:00')"
        )


@pytest.fixture
def v103(env) -> Env:
    asyncio.run(_build_v103(env.engine, env.sessions))
    return env


def snapshot(env: Env) -> dict:
    return {
        "transactions": env.sql(f"SELECT {V103_COLUMNS} FROM transactions ORDER BY id"),
        **{table: env.sql(f"SELECT * FROM {table} ORDER BY 1") for table in PRESERVED_TABLES},
    }


def performer_column(env: Env) -> tuple | None:
    columns = {row[1]: row for row in env.sql("PRAGMA table_info(transactions)")}
    return columns.get(COLUMN)


def performer_foreign_keys(env: Env) -> list[tuple]:
    return [
        (row[2], row[3], row[4], row[5], row[6])
        for row in env.sql("PRAGMA foreign_key_list(transactions)")
        if row[3] == COLUMN
    ]


def run_entry(entry: str) -> None:
    from backend import desktop

    if entry == "web":
        with TestClient(app):  # the startup hook
            pass
    elif entry == "init_db":
        asyncio.run(seed.main())
    elif entry == "desktop":
        asyncio.run(desktop._seed_database())
    elif entry == "upgrade":
        asyncio.run(database.upgrade_existing_database())
    else:  # pragma: no cover - guards the parametrisation
        raise AssertionError(entry)


ENTRIES = ["web", "init_db", "desktop", "upgrade"]


# ---------------------------------------------------------------------------
# 1. Fresh schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["init_db", "desktop"])
def test_fresh_schema_has_a_nullable_performer_fk_that_restricts_deletes(env, entry) -> None:
    run_entry(entry)
    column = performer_column(env)
    assert column is not None
    # (cid, name, type, notnull, dflt_value, pk)
    assert column[2] == "INTEGER"
    assert column[3] == 0
    assert column[4] is None
    assert performer_foreign_keys(env) == [("users", COLUMN, "id", "NO ACTION", "RESTRICT")]


def test_restrict_rule_refuses_to_delete_a_user_with_history(env) -> None:
    run_entry("init_db")
    env.sql("SELECT 1")  # connection warm-up keeps PRAGMA foreign_keys=ON

    async def attribute() -> int:
        async with env.sessions() as session:
            user = User(username="sara", password_hash="x", role="seller")
            session.add(user)
            await session.flush()
            session.add(Transaction(
                service_id=1, amount_cents=100, status="successful", ussd_string="*1#",
                reference="TXN-restrict", customer_phone="0933123456", performed_by_user_id=user.id,
            ))
            await session.commit()
            return user.id

    user_id = asyncio.run(attribute())

    async def delete() -> None:
        async with env.engine.begin() as connection:
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    with pytest.raises(IntegrityError):
        asyncio.run(delete())
    assert env.sql("SELECT performed_by_user_id FROM transactions") == [(user_id,)]


# ---------------------------------------------------------------------------
# 2-3. v1.0.3 upgrade through every startup path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRIES)
def test_v103_database_upgrades_on_every_startup_path(v103, entry) -> None:
    before = snapshot(v103)
    assert performer_column(v103) is None
    assert {row[0] for row in v103.sql("SELECT name FROM sqlite_master WHERE type='table'")}.isdisjoint(
        AUTH_TABLES
    )

    run_entry(entry)

    # The column exists, nullable, with the history-preserving rule...
    column = performer_column(v103)
    assert column is not None and column[2] == "INTEGER" and column[3] == 0 and column[4] is None
    assert performer_foreign_keys(v103) == [("users", COLUMN, "id", "NO ACTION", "RESTRICT")]
    # ...every legacy value is unchanged and no legacy row was back-filled...
    assert snapshot(v103) == before
    assert v103.sql("SELECT id, performed_by_user_id FROM transactions ORDER BY id") == [
        (1, None), (2, None), (3, None),
    ]
    assert v103.sql("PRAGMA foreign_key_check") == []
    assert v103.sql("PRAGMA integrity_check") == [("ok",)]

    # ...and every later start (on any path) is a no-op.
    schema = v103.sql("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
    for again in ENTRIES:
        run_entry(again)
    assert v103.sql("SELECT type, name, sql FROM sqlite_master ORDER BY type, name") == schema
    assert snapshot(v103) == before
    assert asyncio.run(migrate_transaction_lifecycle(v103.engine)) is False


def test_attribution_step_reports_its_change_once(v103) -> None:
    assert asyncio.run(migrate_transaction_lifecycle(v103.engine)) is True
    assert asyncio.run(migrate_transaction_lifecycle(v103.engine)) is False


def test_upgraded_schema_matches_a_fresh_install(v103, tmp_path) -> None:
    run_entry("upgrade")
    fresh = Env(_engine(tmp_path / "fresh.db"), None)
    try:
        async def create() -> None:
            async with fresh.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

        asyncio.run(create())
        # v1.0.5 adds a fresh-install-only name snapshot, not a seller-data migration.
        fresh_columns = fresh.sql("PRAGMA table_info(transactions)")
        assert fresh_columns[-1][1:4] == ("customer_name", "VARCHAR(255)", 0)
        assert current_transaction_columns(v103.sql("PRAGMA table_info(transactions)")) == current_transaction_columns(fresh_columns[:-1])
        # Compared by definition: SQLite numbers a column-level foreign key
        # added by ALTER TABLE before the table-level ones, so only the
        # internal ordinal (first field) may differ.
        assert sorted(row[1:] for row in v103.sql("PRAGMA foreign_key_list(transactions)") if row[2] != "customers") == sorted(
            row[1:] for row in fresh.sql("PRAGMA foreign_key_list(transactions)")
        )
    finally:
        asyncio.run(fresh.engine.dispose())


@pytest.mark.parametrize(
    "definition",
    [
        "performed_by_user_id TEXT",  # wrong type, no foreign key
        "performed_by_user_id INTEGER REFERENCES users (id)",  # deletes would not be restricted
        "performed_by_user_id INTEGER REFERENCES customers (id) ON DELETE RESTRICT",  # wrong target
        "performed_by_user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users (id) ON DELETE RESTRICT",
    ],
)
def test_an_incompatible_existing_performer_column_fails_closed(v103, definition) -> None:
    import sqlite3

    # Plain sqlite3 leaves foreign keys off, so even a NOT NULL REFERENCES
    # column can be planted, as a hand-edited or foreign build might have.
    asyncio.run(v103.engine.dispose())
    raw = sqlite3.connect(v103.engine.url.database)
    try:
        raw.execute(f"ALTER TABLE transactions ADD COLUMN {definition}")
        raw.commit()
    finally:
        raw.close()
    schema = v103.sql("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
    before = snapshot(v103)
    with pytest.raises(SchemaUpgradeError):
        asyncio.run(migrate_transaction_lifecycle(v103.engine))
    with pytest.raises(SchemaUpgradeError):
        asyncio.run(database.upgrade_existing_database())
    assert v103.sql("SELECT type, name, sql FROM sqlite_master ORDER BY type, name") == schema
    assert snapshot(v103) == before


# ---------------------------------------------------------------------------
# API: attribution, forgery, replay, filtering
# ---------------------------------------------------------------------------


@dataclass
class World:
    sessions: async_sessionmaker
    admin: TestClient
    seller: TestClient
    admin_id: int
    seller_id: int
    other_seller_id: int

    def run(self, factory):
        async def runner():
            async with self.sessions() as session:
                return await factory(session)
        return asyncio.run(runner())

    def performer_of(self, transaction_id: int) -> int | None:
        async def read(session):
            return await session.scalar(
                select(Transaction.performed_by_user_id).where(Transaction.id == transaction_id)
            )
        return self.run(read)

    def counts(self) -> dict:
        async def read(session):
            return {
                "transactions": await session.scalar(select(func.count()).select_from(Transaction)),
                "profit": await session.scalar(select(func.count()).select_from(ProfitLedger)),
                "balances": tuple(
                    (await session.execute(
                        select(OperatorBalance.operator_id, OperatorBalance.balance_cents)
                        .order_by(OperatorBalance.operator_id)
                    )).all()
                ),
                # Slice 4.1: the SIM card balances and ledger a sale moves.
                "sim_balances": tuple(
                    (await session.execute(
                        select(SimBalance.sim_card_id, SimBalance.balance_cents)
                        .order_by(SimBalance.sim_card_id)
                    )).all()
                ),
                "sim_ledger": await session.scalar(select(func.count()).select_from(SimLedger)),
            }
        return self.run(read)


def _user_id(sessions: async_sessionmaker, username: str) -> int:
    async def read() -> int:
        async with sessions() as session:
            return await session.scalar(select(User.id).where(User.username == username))
    return asyncio.run(read())


@pytest.fixture
def world(tmp_path) -> Iterator[World]:
    engine = _engine(tmp_path / "world.db")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            await seed.seed_data(session)
            # A second seller who never signs in: still a filter choice.
            session.add(User(username="omar", password_hash="x", role="seller", is_active=False))
            await session.commit()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    asyncio.run(setup())
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as admin, TestClient(app) as seller:
            sign_in(admin, sessions)
            sign_in(seller, sessions, role="seller", username="sara")
            # Slice 4.1: one shared SIM card per company, funded by its own
            # audited top-up; both accounts sell from it.
            WORLD_SIMS.clear()
            for operator_code in ("mtn", "syriatel"):
                WORLD_SIMS[operator_code] = fund_company(admin, operator_code, 1_000_000)
                remember_sim(seller, operator_code, WORLD_SIMS[operator_code])
            yield World(
                sessions=sessions,
                admin=admin,
                seller=seller,
                admin_id=_user_id(sessions, "test-admin"),
                seller_id=_user_id(sessions, "sara"),
                other_seller_id=_user_id(sessions, "omar"),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


#: The shared SIM card of each company in the ``world`` fixture (Slice 4.1).
WORLD_SIMS: dict[str, int] = {}


def sale(**overrides) -> dict:
    body = {
        "customer_phone": "0933123456",
        "operator_code": "mtn",
        "service_code": "prepaid",
        "amount_cents": 1000,
        "commit_key": new_commit_key(),
    }
    body.update(overrides)
    if "sim_card_id" not in body and body["operator_code"] in WORLD_SIMS:
        body["sim_card_id"] = WORLD_SIMS[body["operator_code"]]
    return body


def commit(client: TestClient, **overrides) -> dict:
    response = client.post("/api/transactions", json=sale(**overrides))
    assert response.status_code == 200, response.text
    return response.json()


def add_legacy_row(world: World, reference: str = "TXN-legacy") -> int:
    async def insert(session) -> int:
        row = Transaction(
            service_id=1, amount_cents=900, status="successful", ussd_string="*150*1#",
            reference=reference, customer_phone="0955000000",
        )
        session.add(row)
        await session.commit()
        return row.id
    return world.run(insert)


def listed(client: TestClient, **params) -> list[dict]:
    response = client.get("/api/transactions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_seller_commit_records_the_seller(world) -> None:
    created = commit(world.seller)
    assert world.performer_of(created["id"]) == world.seller_id
    assert created["performed_by_user_id"] == world.seller_id


def test_an_admin_commit_records_the_admin(world) -> None:
    created = commit(world.admin)
    assert world.performer_of(created["id"]) == world.admin_id
    assert created["performed_by_user_id"] == world.admin_id


@pytest.mark.parametrize("field", ["user_id", "performed_by_user_id", "seller_id", "created_by"])
def test_forged_identity_fields_in_the_body_are_rejected_without_side_effects(world, field) -> None:
    before = world.counts()
    response = world.seller.post("/api/transactions", json=sale(**{field: world.admin_id}))
    assert response.status_code == 422
    assert world.counts() == before


def test_identity_in_the_query_string_or_headers_never_changes_the_performer(world) -> None:
    response = world.seller.post(
        "/api/transactions",
        params={"performed_by_user_id": world.admin_id, "user_id": world.admin_id},
        headers={"X-User-Id": str(world.admin_id), "X-Performed-By": str(world.admin_id)},
        json=sale(),
    )
    assert response.status_code == 200, response.text
    assert world.performer_of(response.json()["id"]) == world.seller_id


def test_replay_returns_the_original_row_and_performer_without_side_effects(world) -> None:
    body = sale(customer_name="Ahmad")
    first = world.seller.post("/api/transactions", json=body)
    assert first.status_code == 200
    after_first = world.counts()

    # The same commit key replayed by another account (e.g. a shared device).
    for client in (world.admin, world.seller):
        replay = client.post("/api/transactions", json=body)
        assert replay.status_code == 200
        assert replay.json() == first.json()
    assert world.performer_of(first.json()["id"]) == world.seller_id
    assert world.counts() == after_first


def test_deactivating_a_user_keeps_their_attribution(world) -> None:
    created = commit(world.seller)
    response = world.admin.patch(f"/api/users/{world.seller_id}", json={"is_active": False})
    assert response.status_code == 200, response.text
    assert world.performer_of(created["id"]) == world.seller_id
    rows = listed(world.admin, performed_by=world.seller_id)
    assert [(row["id"], row["performed_by_username"]) for row in rows] == [(created["id"], "sara")]


def test_unfiltered_list_includes_legacy_rows_with_no_performer(world) -> None:
    legacy_id = add_legacy_row(world)
    mine = commit(world.seller)
    rows = {row["id"]: row for row in listed(world.admin)}
    assert set(rows) == {legacy_id, mine["id"]}
    assert rows[legacy_id]["performed_by_user_id"] is None
    assert rows[legacy_id]["performed_by_username"] is None
    assert rows[mine["id"]]["performed_by_user_id"] == world.seller_id
    assert rows[mine["id"]]["performed_by_username"] == "sara"


@pytest.mark.parametrize("viewer", ["admin", "seller"])
def test_both_roles_see_everything_and_can_filter_by_any_performer(world, viewer) -> None:
    client = getattr(world, viewer)
    legacy_id = add_legacy_row(world)
    by_seller = commit(world.seller)
    by_admin = commit(world.admin, operator_code="syriatel")

    assert {row["id"] for row in listed(client)} == {legacy_id, by_seller["id"], by_admin["id"]}
    assert [row["id"] for row in listed(client, performed_by=world.seller_id)] == [by_seller["id"]]
    assert [row["id"] for row in listed(client, performed_by=world.admin_id)] == [by_admin["id"]]
    # A real account with no sales, and an id that does not exist: empty, not an error.
    assert listed(client, performed_by=world.other_seller_id) == []
    assert listed(client, performed_by=987654) == []


def test_performer_filter_combines_with_the_existing_filters(world) -> None:
    mtn = commit(world.seller)
    syriatel = commit(world.seller, operator_code="syriatel", service_code="postpaid",
                      customer_phone="0944123456")
    commit(world.admin)

    assert [r["id"] for r in listed(world.admin, performed_by=world.seller_id, operator_code="mtn")] == [mtn["id"]]
    assert [r["id"] for r in listed(world.admin, performed_by=world.seller_id, service_code="postpaid")] == [
        syriatel["id"]
    ]
    assert [r["id"] for r in listed(world.admin, performed_by=world.seller_id, customer_phone="0944")] == [
        syriatel["id"]
    ]
    assert [r["id"] for r in listed(world.admin, performed_by=world.seller_id, status="successful")] == [
        syriatel["id"], mtn["id"],
    ]
    assert listed(world.admin, performed_by=world.seller_id, status="failed") == []


@pytest.mark.parametrize("value", ["abc", "0", "-1", "1.5", "", "99999999999999999999", "legacy"])
def test_invalid_performer_ids_are_rejected_safely(world, value) -> None:
    commit(world.seller)
    response = world.admin.get("/api/transactions", params={"performed_by": value})
    assert response.status_code == 422
    assert "Traceback" not in response.text


SECRET_MARKERS = ("password", "hash", "scrypt", "session", "csrf", "token", "temporary")


def _mentions_secret(blob: str) -> bool:
    lowered = blob.lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def test_list_and_commit_responses_expose_only_the_performer_id_and_username(world) -> None:
    created = commit(world.seller)
    rows = listed(world.admin)
    assert set(rows[0]) == {
        "id", "reference", "ussd_string", "status", "amount_cents", "created_at",
        "customer_phone", "customer_name", "operator_code", "operator_name",
        "service_code", "service_name", "performed_by_user_id", "performed_by_username",
        # Slice 4.1: what funded the sale (a SIM card or the legacy credit).
        "funding", "sim_card_id", "sim_display_name",
    }
    assert set(created) == {
        "id", "reference", "ussd_string", "status", "amount_cents", "created_at", "performed_by_user_id",
        "sim_card_id", "customer_name",
    }
    assert not _mentions_secret(json.dumps(rows)), "list response mentions credential material"
    assert not _mentions_secret(json.dumps(created)), "commit response mentions credential material"


PERFORMER_SELECT = re.compile(r'<select[^>]*id="filter-performer"[^>]*>(.*?)</select>', re.S)
OPTION = re.compile(r'<option value="([^"]*)"[^>]*>(.*?)</option>', re.S)


def performer_options(html: str) -> list[tuple[str, str]]:
    match = PERFORMER_SELECT.search(html)
    assert match, "the performer filter is missing"
    return [(value, " ".join(label.split())) for value, label in OPTION.findall(match.group(1))]


@pytest.mark.parametrize("viewer", ["admin", "seller"])
def test_page_lists_every_account_including_inactive_ones(world, viewer) -> None:
    response = getattr(world, viewer).get("/transactions")
    assert response.status_code == 200
    options = performer_options(response.text)
    assert options[0] == ("", "All sellers")
    assert options[1:] == sorted(
        [
            (str(world.other_seller_id), "omar"),
            (str(world.seller_id), "sara"),
            (str(world.admin_id), "test-admin"),
        ],
        key=lambda option: option[1],
    )
    select_markup = PERFORMER_SELECT.search(response.text).group(0)
    assert not _mentions_secret(select_markup), "performer options mention credential material"


def test_page_filter_and_column_are_bilingual_and_labelled(world) -> None:
    english = world.admin.get("/transactions").text
    arabic = world.admin.get("/transactions?lang=ar").text
    assert '<label for="filter-performer"' in english
    assert ">Performed by</label>" in english
    assert ">Performed by</th>" in english
    assert performer_options(arabic)[0] == ("", "كل البائعين")
    assert ">نفّذها</label>" in arabic
    assert ">نفّذها</th>" in arabic
    assert 'dir="rtl"' in arabic


def test_usernames_are_escaped_in_the_options(world) -> None:
    async def add(session) -> None:
        session.add(User(username="<b>x</b>", password_hash="x", role="seller"))
        await session.commit()

    world.run(add)
    html = world.admin.get("/transactions").text
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


# ---------------------------------------------------------------------------
# Page script (DOM double): query, column, reset
# ---------------------------------------------------------------------------

ENV = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
PERFORMERS = [{"id": 2, "username": "sara"}, {"id": 1, "username": "test-admin"}]
ROWS = [
    {
        "id": 2, "reference": "TXN-new", "ussd_string": "*150*1#", "status": "successful",
        "amount_cents": 1000, "created_at": "2026-11-05T09:00:00+00:00",
        "customer_phone": "0933123456", "customer_name": None, "operator_code": "mtn",
        "operator_name": "MTN", "service_code": "prepaid", "service_name": "Pre-Paid Charging",
        "performed_by_user_id": 2, "performed_by_username": "sara",
    },
    {
        "id": 1, "reference": "TXN-old", "ussd_string": "*150*2#", "status": "successful",
        "amount_cents": 900, "created_at": "2026-01-05T09:00:00+00:00",
        "customer_phone": "0955000000", "customer_name": None, "operator_code": "mtn",
        "operator_name": "MTN", "service_code": "prepaid", "service_name": "Pre-Paid Charging",
        "performed_by_user_id": None, "performed_by_username": None,
    },
]


def run_page(steps, lang: str = "en") -> dict:
    if not shutil.which("node"):
        pytest.skip("Node.js required to execute the rendered transactions page script")
    html = ENV.get_template("transactions.html").render(
        lang=lang, nav_page="transactions", performers=PERFORMERS
    )
    script = next(s for s in re.findall(r"<script>(.*?)</script>", html, re.S) if "tx-filters" in s)
    result = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps({"html": html, "script": script, "steps": steps, "rows": ROWS}),
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def list_requests(result: dict) -> list[str]:
    return [r["url"] for r in result["requests"] if r["method"] == "GET" and r["url"].startswith("/api/transaction-ledger")]


def test_page_shows_the_performer_and_a_neutral_legacy_label() -> None:
    result = run_page([])
    performers = {row["id"]: row["performer"] for row in result["rows"]}
    assert performers == {"2": "sara", "1": "Legacy / unknown"}
    assert all(row["cells"] == 10 for row in result["rows"])
    # Unfiltered by default: the first request carries no performer filter.
    assert list_requests(result)[0] == "/api/transaction-ledger"

    arabic = run_page([], lang="ar")
    assert {row["id"]: row["performer"] for row in arabic["rows"]} == {"2": "sara", "1": "قديمة / غير معروف"}


def test_choosing_a_performer_queries_the_server_with_the_other_filters() -> None:
    result = run_page([
        {"setFilter": {"id": "filter-operator", "value": "mtn"}},
        {"setFilter": {"id": "filter-type", "value": "sale"}},
        {"setFilter": {"id": "filter-performer", "value": "2"}},
    ])
    assert list_requests(result)[-1] == "/api/transaction-ledger?record_type=sale&operator_code=mtn&performed_by=2"


def test_clear_filters_resets_the_performer_to_all_sellers() -> None:
    result = run_page([
        {"setFilter": {"id": "filter-type", "value": "sale"}},
        {"setFilter": {"id": "filter-performer", "value": "2"}},
        {"click": "tx-reset"},
    ])
    assert result["elements"]["filter-performer"]["value"] == ""
    assert list_requests(result)[-1] == "/api/transaction-ledger"
