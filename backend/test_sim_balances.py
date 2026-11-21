"""v1.0.4 Slice 4.1: per-SIM balances, audited per-SIM top-ups, per-user
default SIM cards, POS SIM selection and SIM-charged sales/refunds.

Contract (see README "Operators and SIM cards"):

* every SIM card starts at 0 and is funded only by an audited per-SIM top-up;
* a committed sale debits exactly the selected SIM card by its face value,
  once; commission/profit/customer amounts are unchanged;
* marking a sale failed refunds the stored original debit to the same SIM
  card, exactly once (even if the SIM was archived since);
* the company "shop credit" (``operator_balance``) of earlier versions is the
  *legacy company credit*: historical, read-only and never charged, refunded,
  split, copied or moved by this slice. Legacy sales keep ``sim_card_id`` NULL
  and are never attributed to a guessed SIM card.

Every scenario runs against a disposable database under ``tmp_path``.
"""
from __future__ import annotations

from backend.historical_test_support import current_transaction_columns

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend import auth, database, main, seed
from backend.auth_test_support import sign_in
from backend.database import User, get_db
from backend.main import app
from backend.migrations import SchemaUpgradeError
from backend.sim_test_support import create_sim, fund_sim, new_key, topup_sim
from backend.test_auth import Env, env, fresh  # noqa: F401 - isolated DB fixtures
from backend.test_operator_sims import add_account, people, user_id  # noqa: F401
from backend.test_transaction_attribution import PRESERVED_TABLES, _build_v103

MARKER = "v1.0.4-sim-balances"
SLICE4_SQL = Path(__file__).with_name("fixtures") / "v104_slice4.sql"
HARNESS = Path(__file__).with_name("pos_result_harness.cjs")
MONEY_HARNESS = Path(__file__).with_name("money_form_harness.cjs")
MAX = 9223372036854775807

SIM_REQUIRED = "Select a SIM card for this company"
SIM_UNAVAILABLE = "This SIM card is not available for this sale"
INSUFFICIENT = "Insufficient SIM balance"
LEGACY_NO_SIM = "This sale was charged to the legacy company credit; it has no SIM card to refund"
LEGACY_READ_ONLY = "Legacy company credit is read-only; top up a SIM card instead"
NEW_TABLES = ("sim_balances", "sim_ledger", "sim_defaults")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def rows(env, statement: str, **params) -> list[tuple]:
    async def run(session):
        return [tuple(row) for row in (await session.execute(text(statement), params)).all()]
    return env.run(run)


def scalar(env, statement: str, **params):
    found = rows(env, statement, **params)
    return found[0][0] if found else None


def balance(env, sim_id: int) -> int | None:
    """The stored SIM balance; asserts it is a genuine SQLite integer."""
    found = rows(env, "SELECT balance_cents, typeof(balance_cents) FROM sim_balances WHERE sim_card_id = :id",
                 id=sim_id)
    if not found:
        return None
    value, kind = found[0]
    assert kind == "integer"
    return value


def ledger(env) -> list[tuple]:
    return rows(env, "SELECT sim_card_id, kind, amount_cents, balance_after_cents, transaction_id,"
                     " performed_by_user_id FROM sim_ledger ORDER BY id")


def legacy_money(env) -> dict:
    """Every legacy money table, with SQLite storage classes of the amounts."""
    return {
        "operator_balance": rows(env, "SELECT *, typeof(balance_cents) FROM operator_balance ORDER BY id"),
        "operator_topup": rows(env, "SELECT *, typeof(amount_cents) FROM operator_topup ORDER BY id"),
        "operator_credit_refund": rows(
            env, "SELECT *, typeof(amount_cents) FROM operator_credit_refund ORDER BY id"),
        "profit_ledger": rows(env, "SELECT *, typeof(amount_cents), typeof(commission_cents),"
                                   " typeof(profit_cents) FROM profit_ledger ORDER BY id"),
    }


def preview(client: TestClient, sim_id, amount: int = 50_000, operator: str = "mtn", **extra):
    body = {"customer_phone": "0944123456", "operator_code": operator,
            "service_code": "prepaid", "amount_cents": amount, **extra}
    if sim_id is not None:
        body["sim_card_id"] = sim_id
    return client.post("/api/transactions/preview", json=body)


def sale(client: TestClient, sim_id, amount: int = 50_000, key: str | None = None,
         operator: str = "mtn", **extra):
    body = {"customer_phone": "0944123456", "operator_code": operator, "service_code": "prepaid",
            "amount_cents": amount, "commit_key": key or new_key("commitkey"), **extra}
    if sim_id is not None:
        body["sim_card_id"] = sim_id
    return client.post("/api/transactions", json=body)


def mark_failed(client: TestClient, transaction_id: int):
    return client.patch(f"/api/transactions/{transaction_id}/status", json={"status": "failed"})


def pos_sims(client: TestClient) -> dict:
    response = client.get("/api/pos/sims")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return {company["operator_code"]: company for company in response.json()["companies"]}


def set_default(client: TestClient, operator_code: str, sim_id):
    return client.put("/api/pos/default-sim", json={"operator_code": operator_code, "sim_id": sim_id})


def default_rows(env) -> list[tuple]:
    return rows(env, "SELECT user_id, operator_id, sim_card_id FROM sim_defaults ORDER BY user_id, operator_id")


def counts(env) -> tuple[int, int, int]:
    return (scalar(env, "SELECT COUNT(*) FROM transactions"),
            scalar(env, "SELECT COUNT(*) FROM sim_ledger"),
            scalar(env, "SELECT COUNT(*) FROM profit_ledger"))


def run_all_startups() -> None:
    from backend import desktop

    asyncio.run(seed.main())
    asyncio.run(desktop._seed_database())
    asyncio.run(database.upgrade_existing_database())
    with TestClient(app):  # the web startup hook
        pass


def markers(env) -> set[str]:
    return {row[0] for row in rows(env, "SELECT version FROM schema_migrations")}


def file_digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path: str) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


@pytest.fixture
def shop(people, fresh):
    """An admin plus two sellers, signed in, with a typical set of SIM cards.

    * ``shared``: shared MTN SIM (funded 1,000,000)
    * ``sara``: Sara's private MTN SIM (funded 1,000,000)
    * ``bob``: Bob's private MTN SIM (funded 1,000,000)
    * ``syr``: shared Syriatel SIM (0)
    """
    admin = people("boss", "admin")
    sara = people("sara")
    bob = people("bob")
    sims = {
        "shared": create_sim(admin, "mtn", visibility="shared", display_name="Shop MTN", phone="0944100001"),
        "sara": create_sim(sara, "mtn", visibility="private", display_name="Sara secret MTN",
                           phone="0944100002"),
        "bob": create_sim(bob, "mtn", visibility="private", display_name="Bob hidden MTN",
                          phone="0944100003"),
        "syr": create_sim(admin, "syriatel", visibility="shared", display_name="Shop Syriatel",
                          phone="0933100004"),
    }
    for name in ("shared", "sara", "bob"):
        assert topup_sim(admin, sims[name]["id"], 1_000_000).status_code == 200
    ids = {name: sim["id"] for name, sim in sims.items()}
    return {"admin": admin, "sara": sara, "bob": bob, "ids": ids, "env": fresh, "people": people}


# ---------------------------------------------------------------------------
# 1. migrations preserve money exactly; no SIM is created, no credit moved
# ---------------------------------------------------------------------------


def _load_slice4(path: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SLICE4_SQL.read_text())
    finally:
        connection.close()


def _slice4_history(env) -> None:
    """A real Slice 4 file with accounts, SIM cards and legacy money in it."""
    _load_slice4(env.engine.url.database)
    owner = add_account(env, "owner", role="admin")
    seller = add_account(env, "seller1")

    async def history(session):
        await session.execute(text(
            "INSERT INTO sim_cards (id, operator_id, display_name, phone, owner_user_id, is_shared, archived_at,"
            " created_by_user_id, created_at, updated_at) VALUES"
            " (1, 1, 'Counter MTN', '0944000001', NULL, 1, NULL, :owner, '2026-11-01 08:00:00',"
            "  '2026-11-01 08:00:00'),"
            " (2, 2, 'Seller Syriatel', '0933000002', :seller, 0, '2026-11-02 08:00:00', :seller,"
            "  '2026-11-01 08:00:00', '2026-11-02 08:00:00')"), {"owner": owner, "seller": seller})
        await session.execute(text("UPDATE operator_balance SET balance_cents = 987654 WHERE operator_id = 1"))
        await session.execute(text("UPDATE operator_balance SET balance_cents = 123400 WHERE operator_id = 2"))
        await session.execute(text(
            "INSERT INTO operator_topup (id, operator_id, amount_cents, reference, created_at) VALUES"
            " (1, 1, 1000000, 'TOPUP-s4-1', '2026-11-01 09:00:00'),"
            " (2, 2, 130000, 'TOPUP-s4-2', '2026-11-01 09:30:00')"))
        await session.execute(text(
            "INSERT INTO transactions (id, customer_id, service_id, amount_cents, status, ussd_string, reference,"
            " created_at, idempotency_key, deleted_at, legacy_status, customer_phone, performed_by_user_id) VALUES"
            " (1, NULL, 1, 12346, 'successful', '*a#', 'TXN-s4-a', '2026-11-03 10:00:00', 'slice4-key-aaaaaaaa',"
            "  NULL, NULL, '0944123456', :seller),"
            " (2, NULL, 3, 6600, 'failed', '*b#', 'TXN-s4-b', '2026-11-03 11:00:00', 'slice4-key-bbbbbbbb',"
            "  NULL, NULL, '0933123456', :owner)"), {"owner": owner, "seller": seller})
        await session.execute(text(
            "INSERT INTO profit_ledger (transaction_id, operator_code, amount_cents, commission_cents, profit_cents,"
            " created_at) VALUES (1, 'mtn', 12346, 617, 617, '2026-11-03 10:00:00'),"
            " (2, 'syriatel', 6600, 330, 330, '2026-11-03 11:00:00')"))
        await session.execute(text(
            "INSERT INTO operator_credit_refund (transaction_id, transaction_reference, operator_id, amount_cents,"
            " created_at) VALUES (2, 'TXN-s4-b', 2, 6600, '2026-11-03 12:00:00')"))
        await session.commit()
    env.run(history)


TRANSACTION_COLUMNS = ("id, customer_id, service_id, amount_cents, typeof(amount_cents), status, ussd_string,"
                       " reference, created_at, idempotency_key, deleted_at, legacy_status, customer_phone")


def _snapshot(env, performer: bool = True) -> dict:
    columns = TRANSACTION_COLUMNS + (", performed_by_user_id" if performer else "")
    return {
        **legacy_money(env),
        **{table: rows(env, f"SELECT * FROM {table} ORDER BY 1") for table in PRESERVED_TABLES},
        "transactions": rows(env, f"SELECT {columns} FROM transactions ORDER BY id"),
        "sim_cards": rows(env, "SELECT * FROM sim_cards ORDER BY id") if "sim_cards" in _tables(env) else [],
    }


def _tables(env) -> set[str]:
    return {row[0] for row in rows(env, "SELECT name FROM sqlite_master WHERE type='table'")}


def _assert_installed_and_empty(env) -> None:
    assert set(NEW_TABLES) <= _tables(env)
    assert MARKER in markers(env)
    for table in NEW_TABLES:
        assert scalar(env, f"SELECT COUNT(*) FROM {table}") == 0
    assert scalar(env, "SELECT COUNT(*) FROM transactions WHERE sim_card_id IS NOT NULL") == 0


def test_v103_upgrade_preserves_every_money_value_and_type(env) -> None:
    asyncio.run(_build_v103(env.engine, env.sessions))
    assert not set(NEW_TABLES) & _tables(env)
    before = _snapshot(env, performer=False)
    assert before["operator_balance"] and before["transactions"]

    run_all_startups()

    assert _snapshot(env, performer=False) == before
    _assert_installed_and_empty(env)
    assert rows(env, "SELECT COUNT(*) FROM sim_cards") == [(0,)]  # no SIM invented
    first = dump(env.engine.url.database)
    run_all_startups()  # idempotent: a second round changes nothing at all
    assert dump(env.engine.url.database) == first


def test_slice4_upgrade_with_sims_and_legacy_money_moves_nothing(env) -> None:
    _slice4_history(env)
    assert not set(NEW_TABLES) & _tables(env)
    columns = {row[1] for row in rows(env, "PRAGMA table_info(transactions)")}
    assert "sim_card_id" not in columns  # a faithful Slice 4 file
    before = _snapshot(env)

    run_all_startups()

    after = _snapshot(env)
    # Default initialization inserts the two fresh independent settings only.
    # Every historical row (including the unused shared rate) is unchanged.
    additions = [row for row in after["settings"] if row[1].startswith("commission_rate_")]
    assert [(row[1], row[2]) for row in additions] == [
        ("commission_rate_prepaid", "0.20"), ("commission_rate_postpaid", "0.06")]
    after["settings"] = [row for row in after["settings"] if row not in additions]
    assert after == before
    _assert_installed_and_empty(env)
    # This old-schema fixture needs the fresh-only v1.0.5 column before
    # exercising current API serialization; no production migration is added.
    async def current_fixture_schema():
        async with env.engine.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE transactions ADD COLUMN customer_name VARCHAR(255)"
            )

    asyncio.run(current_fixture_schema())
    # The legacy company credit still reads exactly as before, as legacy.
    with TestClient(app) as client:
        sign_in(client, env.sessions, username="owner")
        assert {b["operator_code"]: b["balance_cents"] for b in client.get("/api/operators/balance").json()} == {
            "mtn": 987654, "syriatel": 123400}
        sims = {sim["id"]: sim for sim in client.get("/api/sims?include_archived=true").json()}
        assert sims[1]["balance_cents"] == 0 and sims[2]["balance_cents"] == 0
        listed = {item["reference"]: item for item in client.get("/api/transactions").json()}
        assert listed["TXN-s4-a"]["sim_card_id"] is None and listed["TXN-s4-a"]["funding"] == "legacy"
    first = dump(env.engine.url.database)
    run_all_startups()
    assert dump(env.engine.url.database) == first


@pytest.mark.parametrize("entry", ["source", "desktop"])
def test_fresh_install_matches_an_upgraded_schema(env, tmp_path, entry) -> None:
    from backend import desktop

    asyncio.run(seed.main() if entry == "source" else desktop._seed_database())
    _assert_installed_and_empty(env)

    def shape(path: str) -> dict:
        connection = sqlite3.connect(path)
        try:
            return {
                "transactions": connection.execute("PRAGMA table_info(transactions)").fetchall(),
                "fk": sorted(row[2:] for row in connection.execute("PRAGMA foreign_key_list(transactions)")),
                **{t: connection.execute(f"SELECT sql FROM sqlite_master WHERE name = '{t}'").fetchall()
                   for t in NEW_TABLES},
                "triggers": connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall(),
            }
        finally:
            connection.close()

    fresh_shape = shape(env.engine.url.database)
    # v1.0.5 adds a fresh-install-only snapshot, outside this historical migration.
    assert fresh_shape["transactions"][-1][1:4] == ("customer_name", "VARCHAR(255)", 0)
    fresh_shape["transactions"] = fresh_shape["transactions"][:-1]
    upgraded = tmp_path / "upgraded.db"
    _load_slice4(str(upgraded))
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(upgraded)))
    event.listen(engine.sync_engine, "connect", database.enable_sqlite_foreign_keys)
    try:
        assert asyncio.run(database.migrate_sim_balances(engine)) is True
        assert asyncio.run(database.migrate_sim_balances(engine)) is False
    finally:
        asyncio.run(engine.dispose())
    upgraded_shape = shape(str(upgraded))
    for shape_ in (fresh_shape, upgraded_shape):
        shape_["transactions"] = current_transaction_columns(shape_["transactions"])
        shape_["fk"] = [row for row in shape_["fk"] if row[0] != "customers"]
    sim_column = [row for row in upgraded_shape["transactions"] if row[0] == "sim_card_id"]
    assert sim_column == [("sim_card_id", "INTEGER", 0, None, 0)]
    assert ("sim_cards", "sim_card_id", "id", "NO ACTION", "RESTRICT", "NONE") in upgraded_shape["fk"]
    assert upgraded_shape["fk"] == fresh_shape["fk"]
    assert upgraded_shape["transactions"] == fresh_shape["transactions"]
    assert {t: upgraded_shape[t] for t in NEW_TABLES} == {t: fresh_shape[t] for t in NEW_TABLES}
    assert upgraded_shape["triggers"] == fresh_shape["triggers"]
    assert {name for name, _sql in fresh_shape["triggers"]} == {"sim_ledger_no_update", "sim_ledger_no_delete"}


INCOMPATIBLE = {
    "weak-ledger": [
        "CREATE TABLE sim_ledger (id INTEGER PRIMARY KEY, sim_card_id INTEGER, kind TEXT, amount_cents INTEGER,"
        " balance_after_cents INTEGER, transaction_id INTEGER, idempotency_key TEXT, reference TEXT,"
        " performed_by_user_id INTEGER, created_at DATETIME)"],
    "text-column": ["ALTER TABLE transactions ADD COLUMN sim_card_id TEXT"],
    "column-without-fk": ["ALTER TABLE transactions ADD COLUMN sim_card_id INTEGER"],
    "marker-without-tables": [
        "INSERT INTO schema_migrations(version) VALUES ('v1.0.4-sim-balances')"],
    "balance-without-check": [
        "CREATE TABLE sim_balances (sim_card_id INTEGER NOT NULL, balance_cents INTEGER NOT NULL,"
        " updated_at DATETIME NOT NULL, PRIMARY KEY (sim_card_id), FOREIGN KEY(sim_card_id)"
        " REFERENCES sim_cards (id) ON DELETE RESTRICT)"],
}


@pytest.mark.parametrize("entry", ["web-upgrade", "source", "desktop"])
@pytest.mark.parametrize("damage", sorted(INCOMPATIBLE))
def test_incompatible_sim_balance_schema_fails_closed_without_touching_the_file(env, entry, damage) -> None:
    from backend import desktop

    _slice4_history(env)
    path = env.engine.url.database
    connection = sqlite3.connect(path)
    try:
        for statement in INCOMPATIBLE[damage]:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
    asyncio.run(env.engine.dispose())
    digest = file_digest(path)
    with pytest.raises(SchemaUpgradeError):
        if entry == "web-upgrade":
            asyncio.run(database.upgrade_existing_database())
        elif entry == "source":
            asyncio.run(seed.main())
        else:
            asyncio.run(desktop._seed_database())
    asyncio.run(env.engine.dispose())
    assert file_digest(path) == digest


def test_a_missing_append_only_trigger_fails_closed(env) -> None:
    asyncio.run(seed.main())
    path = env.engine.url.database
    asyncio.run(env.engine.dispose())
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER sim_ledger_no_delete")
    connection.commit()
    connection.close()
    digest = file_digest(path)
    with pytest.raises(SchemaUpgradeError):
        asyncio.run(database.upgrade_existing_database())
    asyncio.run(env.engine.dispose())
    assert file_digest(path) == digest


# ---------------------------------------------------------------------------
# 2. SIM cards start at zero and are funded only by an audited top-up
# ---------------------------------------------------------------------------


def test_new_sim_starts_at_zero_and_topup_is_audited(people, fresh) -> None:
    admin = people("boss", "admin")
    sim = create_sim(admin, "mtn")
    assert sim["balance_cents"] == 0
    assert pos_sims(admin)["mtn"]["sims"][0]["balance_cents"] == 0
    legacy_before = legacy_money(fresh)

    key = new_key("topupkey")
    first = topup_sim(admin, sim["id"], 1_000_000, key)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    body = first.json()
    assert body["sim_id"] == sim["id"] and body["amount_cents"] == 1_000_000
    assert body["balance_cents"] == 1_000_000 and body["reference"].startswith("SIMTOPUP-")
    assert balance(fresh, sim["id"]) == 1_000_000
    boss = user_id(fresh, "boss")
    assert ledger(fresh) == [(sim["id"], "topup", 1_000_000, 1_000_000, None, boss)]
    assert rows(fresh, "SELECT reference, idempotency_key, created_at IS NOT NULL FROM sim_ledger") == [
        (body["reference"], key, 1)]

    # A repeated submit (same key) never credits twice and answers the original.
    again = topup_sim(admin, sim["id"], 1_000_000, key)
    assert again.status_code == 200 and again.json()["reference"] == body["reference"]
    assert balance(fresh, sim["id"]) == 1_000_000 and len(ledger(fresh)) == 1
    # The same key for a different top-up is refused, nothing written.
    assert topup_sim(admin, sim["id"], 5, key).status_code == 409
    second = topup_sim(admin, sim["id"], 250)
    assert second.json()["balance_cents"] == 1_000_250
    assert [row[3] for row in ledger(fresh)] == [1_000_000, 1_000_250]
    assert admin.get(f"/api/sims/{sim['id']}").json()["balance_cents"] == 1_000_250
    # No company credit moved, in either direction.
    assert legacy_money(fresh) == legacy_before


@pytest.mark.parametrize("amount", [0, -1, 1.5, "100", True, MAX + 1, None])
def test_invalid_topup_amounts_are_rejected(people, fresh, amount) -> None:
    admin = people("boss", "admin")
    sim = create_sim(admin)
    response = admin.post(f"/api/sims/{sim['id']}/topup", json={"amount_cents": amount, "topup_key": new_key()})
    assert response.status_code == 422
    assert ledger(fresh) == [] and balance(fresh, sim["id"]) in (None, 0)


def test_topup_requires_a_valid_key_and_no_extra_fields(people, fresh) -> None:
    admin = people("boss", "admin")
    sim = create_sim(admin)
    url = f"/api/sims/{sim['id']}/topup"
    assert admin.post(url, json={"amount_cents": 100}).status_code == 422
    assert admin.post(url, json={"amount_cents": 100, "topup_key": "short"}).status_code == 422
    assert admin.post(url, json={"amount_cents": 100, "topup_key": "bad key with spaces!!"}).status_code == 422
    assert admin.post(url, json={"amount_cents": 100, "topup_key": new_key(),
                                 "performed_by_user_id": 1}).status_code == 422
    assert ledger(fresh) == []


def test_topup_overflow_is_refused_without_change(people, fresh) -> None:
    admin = people("boss", "admin")
    sim = create_sim(admin)
    assert topup_sim(admin, sim["id"], MAX).status_code == 200
    assert topup_sim(admin, sim["id"], 1).status_code == 409
    assert balance(fresh, sim["id"]) == MAX and len(ledger(fresh)) == 1


def test_legacy_company_credit_is_read_only(people, fresh) -> None:
    admin = people("boss", "admin")
    before = legacy_money(fresh)
    response = admin.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 1000})
    assert response.status_code == 409
    assert response.json()["detail"] == LEGACY_READ_ONLY
    assert legacy_money(fresh) == before
    assert admin.get("/api/operators/balance").status_code == 200  # still readable


def test_who_may_top_up_which_sim(shop) -> None:
    ids, env = shop["ids"], shop["env"]
    sara, bob = shop["sara"], shop["bob"]
    assert topup_sim(sara, ids["sara"], 100).status_code == 200
    assert topup_sim(sara, ids["shared"], 100).status_code == 200
    # Another seller's private SIM is invisible: 404, identical to a missing id.
    hidden = topup_sim(sara, ids["bob"], 100)
    missing = topup_sim(sara, 999_999, 100)
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert balance(env, ids["bob"]) == 1_000_000
    # Archived SIM cards cannot be topped up (by the owner or an admin).
    assert bob.post(f"/api/sims/{ids['bob']}/archive").status_code == 200
    assert topup_sim(bob, ids["bob"], 100).status_code == 409
    assert topup_sim(shop["admin"], ids["bob"], 100).status_code == 409
    assert balance(env, ids["bob"]) == 1_000_000


# ---------------------------------------------------------------------------
# 3. per-user default SIM card, one per company
# ---------------------------------------------------------------------------


def test_default_sim_rules(shop) -> None:
    ids, env, sara = shop["ids"], shop["env"], shop["sara"]
    sara_id = user_id(env, "sara")
    assert pos_sims(sara)["mtn"]["default_sim_id"] is None

    saved = set_default(sara, "mtn", ids["sara"])
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"operator_code": "mtn", "default_sim_id": ids["sara"]}
    assert pos_sims(sara)["mtn"]["default_sim_id"] == ids["sara"]
    # One default per (user, company): saving again replaces it.
    assert set_default(sara, "mtn", ids["shared"]).status_code == 200
    assert default_rows(env) == [(sara_id, 1, ids["shared"])]
    # Forbidden choices are refused identically, nothing is written.
    refusals = [
        set_default(sara, "mtn", ids["bob"]),       # another seller's private SIM
        set_default(sara, "mtn", 999_999),          # does not exist
        set_default(sara, "syriatel", ids["sara"]),  # wrong company
    ]
    assert {r.status_code for r in refusals} == {400}
    assert {r.json()["detail"] for r in refusals} == {SIM_UNAVAILABLE}
    assert default_rows(env) == [(sara_id, 1, ids["shared"])]
    # Clearing.
    assert set_default(sara, "mtn", None).json() == {"operator_code": "mtn", "default_sim_id": None}
    assert default_rows(env) == []
    assert set_default(sara, "nokia", None).status_code == 400
    assert sara.put("/api/pos/default-sim", json={"operator_code": "mtn", "sim_id": ids["sara"],
                                                  "user_id": 1}).status_code == 422


def test_archiving_a_sim_clears_every_default_on_it(shop) -> None:
    ids, env = shop["ids"], shop["env"]
    for client in (shop["sara"], shop["bob"], shop["admin"]):
        assert set_default(client, "mtn", ids["shared"]).status_code == 200
    assert len(default_rows(env)) == 3
    assert shop["admin"].post(f"/api/sims/{ids['shared']}/archive").status_code == 200
    assert default_rows(env) == []
    assert set_default(shop["sara"], "mtn", ids["shared"]).status_code == 400  # archived
    assert shop["admin"].post(f"/api/sims/{ids['shared']}/restore").status_code == 200
    assert default_rows(env) == []  # a restore never resurrects a default
    assert pos_sims(shop["sara"])["mtn"]["default_sim_id"] is None


def test_visibility_and_owner_changes_revalidate_defaults(shop) -> None:
    ids, env, admin = shop["ids"], shop["env"], shop["admin"]
    sara_id, bob_id, boss_id = (user_id(env, name) for name in ("sara", "bob", "boss"))
    for client in (shop["sara"], shop["bob"], admin):
        assert set_default(client, "mtn", ids["shared"]).status_code == 200
    # Shared -> Sara's private: Bob loses it, Sara and the admin keep it.
    assert admin.patch(f"/api/sims/{ids['shared']}",
                       json={"visibility": "private", "owner_user_id": sara_id}).status_code == 200
    assert default_rows(env) == sorted([(sara_id, 1, ids["shared"]), (boss_id, 1, ids["shared"])])
    assert pos_sims(shop["bob"])["mtn"]["default_sim_id"] is None
    assert "Shop MTN" not in shop["bob"].get("/api/pos/sims").text
    # Owner Sara -> Bob: Sara loses it.
    assert admin.patch(f"/api/sims/{ids['shared']}", json={"owner_user_id": bob_id}).status_code == 200
    assert default_rows(env) == [(boss_id, 1, ids["shared"])]
    assert pos_sims(shop["sara"])["mtn"]["default_sim_id"] is None


def test_a_demoted_admin_never_sees_a_stale_private_default(shop) -> None:
    ids, env, admin = shop["ids"], shop["env"], shop["admin"]
    assert set_default(admin, "mtn", ids["bob"]).status_code == 200  # admins may use every SIM

    async def demote(session):
        await session.execute(text("UPDATE users SET role = 'seller' WHERE username = 'boss'"))
        await session.commit()
    env.run(demote)
    companies = pos_sims(admin)
    assert companies["mtn"]["default_sim_id"] is None
    assert ids["bob"] not in {sim["id"] for sim in companies["mtn"]["sims"]}
    assert "Bob hidden MTN" not in admin.get("/api/pos/sims").text
    assert sale(admin, ids["bob"], 100).status_code == 400


# ---------------------------------------------------------------------------
# 4 + 10. POS sees authorized SIM cards only; forged ids are rejected
# ---------------------------------------------------------------------------


def test_pos_lists_only_authorized_active_sims(shop) -> None:
    ids = shop["ids"]
    seller = pos_sims(shop["sara"])
    assert [company for company in seller] == ["mtn", "syriatel"]
    # Ordered by name ("Sara secret MTN" before "Shop MTN"), never another seller's.
    assert [sim["id"] for sim in seller["mtn"]["sims"]] == [ids["sara"], ids["shared"]]
    assert [sim["id"] for sim in seller["syriatel"]["sims"]] == [ids["syr"]]
    raw = shop["sara"].get("/api/pos/sims").text
    assert "Bob hidden MTN" not in raw and "0944100003" not in raw
    first = seller["mtn"]["sims"][0]
    assert set(first) == {"id", "display_name", "phone", "visibility", "balance_cents"}
    assert {sim["id"]: sim["balance_cents"] for sim in seller["mtn"]["sims"]} == {
        ids["shared"]: 1_000_000, ids["sara"]: 1_000_000}

    admin = pos_sims(shop["admin"])
    assert {sim["id"] for sim in admin["mtn"]["sims"]} == {ids["shared"], ids["sara"], ids["bob"]}
    # Archived SIM cards are never offered, not even to their owner or an admin.
    assert shop["sara"].post(f"/api/sims/{ids['sara']}/archive").status_code == 200
    assert ids["sara"] not in {sim["id"] for sim in pos_sims(shop["sara"])["mtn"]["sims"]}
    assert ids["sara"] not in {sim["id"] for sim in pos_sims(shop["admin"])["mtn"]["sims"]}
    # The POS page itself never renders another seller's private SIM.
    page = shop["sara"].get("/pos").text
    assert "Bob hidden MTN" not in page and "0944100003" not in page
    assert "Shop MTN" in page


def _forged(shop, archived_id: int) -> dict[str, int]:
    return {"other seller's private": shop["ids"]["bob"], "missing": 999_999,
            "archived": archived_id, "wrong company": shop["ids"]["syr"]}


@pytest.mark.parametrize("call", ["preview", "commit"])
def test_forged_private_archived_or_foreign_sim_ids_are_rejected_before_any_write(shop, call) -> None:
    sara, env, ids = shop["sara"], shop["env"], shop["ids"]
    archived = create_sim(sara, "mtn", visibility="private", display_name="Sara old")
    assert topup_sim(sara, archived["id"], 100_000).status_code == 200
    assert sara.post(f"/api/sims/{archived['id']}/archive").status_code == 200
    before = (counts(env), {sim: balance(env, sim) for sim in (*ids.values(), archived["id"])}, legacy_money(env))
    responses = []
    for sim_id in _forged(shop, archived["id"]).values():
        response = preview(sara, sim_id, 100) if call == "preview" else sale(sara, sim_id, 100)
        responses.append(response)
        assert "Bob hidden MTN" not in response.text and "0944100003" not in response.text
    assert {r.status_code for r in responses} == {400}
    assert {r.json()["detail"] for r in responses} == {SIM_UNAVAILABLE}
    after = (counts(env), {sim: balance(env, sim) for sim in (*ids.values(), archived["id"])}, legacy_money(env))
    assert after == before


def test_sales_without_a_sim_never_fall_back_to_legacy_credit(shop) -> None:
    env, sara = shop["env"], shop["sara"]

    async def legacy_rich(session):
        await session.execute(text("UPDATE operator_balance SET balance_cents = 99999999"))
        await session.commit()
    env.run(legacy_rich)
    before = (counts(env), legacy_money(env))
    for response in (preview(sara, None, 100), sale(sara, None, 100)):
        assert response.status_code == 400
        assert response.json()["detail"] == SIM_REQUIRED
    for bad in (0, -1, "1", 1.5, MAX + 1):
        assert sale(sara, bad, 100).status_code == 422
    assert (counts(env), legacy_money(env)) == before


# ---------------------------------------------------------------------------
# 5-7. preview has no side effects; a commit debits the face value once
# ---------------------------------------------------------------------------


def test_preview_has_no_side_effects(shop) -> None:
    env, sara, ids = shop["env"], shop["sara"], shop["ids"]
    before = (counts(env), balance(env, ids["sara"]), legacy_money(env))
    response = preview(sara, ids["sara"])
    assert response.status_code == 200, response.text
    assert response.json()["ussd_string"] and response.json()["commit_key"]
    assert (counts(env), balance(env, ids["sara"]), legacy_money(env)) == before
    short = preview(sara, ids["syr"], 100, operator="syriatel")
    assert short.status_code == 400 and short.json()["detail"] == INSUFFICIENT
    assert (counts(env), balance(env, ids["sara"]), legacy_money(env)) == before


def test_commit_debits_exactly_the_face_value_of_the_selected_sim(shop) -> None:
    env, sara, ids = shop["env"], shop["sara"], shop["ids"]
    sara_id = user_id(env, "sara")
    legacy_before = legacy_money(env)["operator_balance"]
    others = {name: balance(env, ids[name]) for name in ("shared", "bob", "syr")}

    response = sale(sara, ids["sara"], 50_000)
    assert response.status_code == 200, response.text
    committed = response.json()
    assert committed["sim_card_id"] == ids["sara"]
    assert committed["performed_by_user_id"] == sara_id
    assert balance(env, ids["sara"]) == 950_000
    assert {name: balance(env, ids[name]) for name in others} == others
    assert legacy_money(env)["operator_balance"] == legacy_before  # legacy credit untouched
    # Commission/profit are computed at the prepaid default (20% of the face value).
    assert rows(env, "SELECT amount_cents, commission_cents, profit_cents FROM profit_ledger"
                     " WHERE transaction_id = :id", id=committed["id"]) == [(50_000, 10_000, 10_000)]
    assert rows(env, "SELECT sim_card_id, performed_by_user_id, amount_cents FROM transactions WHERE id = :id",
                id=committed["id"]) == [(ids["sara"], sara_id, 50_000)]
    sale_rows = [row for row in ledger(env) if row[1] == "sale"]
    assert sale_rows == [(ids["sara"], "sale", 50_000, 950_000, committed["id"], sara_id)]
    reference = scalar(env, "SELECT reference FROM sim_ledger WHERE kind = 'sale'")
    assert reference == f"SALE-{committed['reference']}"


def test_exact_balance_and_insufficient_balance(shop) -> None:
    env, sara, ids = shop["env"], shop["sara"], shop["ids"]
    assert sale(sara, ids["sara"], 1_000_000).status_code == 200
    assert balance(env, ids["sara"]) == 0
    before = counts(env)
    response = sale(sara, ids["sara"], 1)
    assert response.status_code == 400 and response.json()["detail"] == INSUFFICIENT
    assert counts(env) == before and balance(env, ids["sara"]) == 0
    # A zero-value sale needs no balance and records a zero debit.
    zero = sale(sara, ids["sara"], 0)
    assert zero.status_code == 200
    assert balance(env, ids["sara"]) == 0
    assert ledger(env)[-1][1:4] == ("sale", 0, 0)


# ---------------------------------------------------------------------------
# 8 + 9. idempotent replays and races
# ---------------------------------------------------------------------------


def test_replay_with_another_sim_returns_the_original_sale_and_sim(shop) -> None:
    env, sara, ids = shop["env"], shop["sara"], shop["ids"]
    key = new_key("commitkey")
    first = sale(sara, ids["sara"], 40_000, key)
    assert first.status_code == 200
    replay = sale(sara, ids["shared"], 40_000, key)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["sim_card_id"] == ids["sara"]
    assert balance(env, ids["sara"]) == 960_000 and balance(env, ids["shared"]) == 1_000_000
    assert len([row for row in ledger(env) if row[1] == "sale"]) == 1
    # Even after the original SIM is archived, a replay answers the stored sale.
    assert sara.post(f"/api/sims/{ids['sara']}/archive").status_code == 200
    late = sale(sara, ids["shared"], 40_000, key)
    assert late.status_code == 200 and late.json()["sim_card_id"] == ids["sara"]
    assert balance(env, ids["shared"]) == 1_000_000


def test_a_stale_replay_check_rereads_the_key_before_reporting_insufficient(shop, monkeypatch) -> None:
    """The losing request of a same-key race saw no stored sale yet; by the time
    it debits, the winner has spent the balance. It must answer the winner's
    sale, not "insufficient"."""
    env, sara, ids = shop["env"], shop["sara"], shop["ids"]
    key = new_key("commitkey")
    winner = sale(sara, ids["sara"], 1_000_000, key)
    assert winner.status_code == 200 and balance(env, ids["sara"]) == 0

    real = main._committed_transaction
    calls = {"n": 0}

    async def stale_first(db, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the snapshot taken before the winner committed
        return await real(db, data)

    monkeypatch.setattr(main, "_committed_transaction", stale_first)
    loser = sale(sara, ids["sara"], 1_000_000, key)
    assert loser.status_code == 200, loser.text
    assert loser.json()["id"] == winner.json()["id"]
    assert balance(env, ids["sara"]) == 0
    assert len([row for row in ledger(env) if row[1] == "sale"]) == 1


def test_a_stale_replay_check_with_funds_left_still_charges_once(shop, monkeypatch) -> None:
    env, sara, ids = shop["env"], shop["sara"], shop["ids"]
    key = new_key("commitkey")
    winner = sale(sara, ids["sara"], 10_000, key)
    real = main._committed_transaction
    calls = {"n": 0}

    async def stale_first(db, data):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(db, data)

    monkeypatch.setattr(main, "_committed_transaction", stale_first)
    loser = sale(sara, ids["sara"], 10_000, key)
    assert loser.status_code == 200 and loser.json()["id"] == winner.json()["id"]
    assert balance(env, ids["sara"]) == 990_000
    assert len([row for row in ledger(env) if row[1] == "sale"]) == 1


@pytest.fixture
def racing(tmp_path, monkeypatch) -> Iterator[Env]:
    """Every request gets its own connection (NullPool), so threads really race."""
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "race.db")),
                                 poolclass=NullPool, connect_args={"timeout": 30})
    event.listen(engine.sync_engine, "connect", database.enable_sqlite_foreign_keys)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "DATABASE_URL", engine.url)
    monkeypatch.setattr(seed, "SessionLocal", sessions)
    asyncio.run(seed.main())

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        yield Env(engine, sessions)
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


def _race(racing, amount: int, keys: list[str], funded: int, call=sale):
    add_account(racing, "owner", role="admin")
    with TestClient(app) as setup:
        sign_in(setup, racing.sessions, username="owner")
        sim_id = fund_sim(setup, "mtn", funded)
    barrier = threading.Barrier(len(keys))
    results: list = [None] * len(keys)

    def attempt(index: int, key: str) -> None:
        with TestClient(app) as client:
            sign_in(client, racing.sessions, username="owner")
            barrier.wait()
            response = call(client, sim_id, amount, key)
            results[index] = (response.status_code, response.json())

    threads = [threading.Thread(target=attempt, args=(i, key)) for i, key in enumerate(keys)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return sim_id, results


def test_two_different_sales_race_for_one_balance(racing) -> None:
    sim_id, results = _race(racing, 30_000, [new_key("racekey"), new_key("racekey")], funded=50_000)
    assert sorted(status for status, _ in results) == [200, 400]
    assert [body["detail"] for status, body in results if status == 400] == [INSUFFICIENT]
    assert balance(racing, sim_id) == 20_000
    assert [row[1:4] for row in ledger(racing) if row[1] == "sale"] == [("sale", 30_000, 20_000)]
    assert scalar(racing, "SELECT COUNT(*) FROM transactions") == 1


def test_the_same_sale_racing_itself_is_charged_once(racing) -> None:
    key = new_key("racekey")
    sim_id, results = _race(racing, 50_000, [key, key, key], funded=50_000)
    assert [status for status, _ in results] == [200, 200, 200], results
    assert len({body["id"] for _, body in results}) == 1
    assert balance(racing, sim_id) == 0
    assert len([row for row in ledger(racing) if row[1] == "sale"]) == 1


# ---------------------------------------------------------------------------
# 11-13. refunds: exactly once, to the same SIM; legacy and archived SIMs
# ---------------------------------------------------------------------------


def test_failed_sale_refunds_the_stored_debit_exactly_once(shop) -> None:
    env, sara, bob, ids = shop["env"], shop["sara"], shop["bob"], shop["ids"]
    sara_id, bob_id = user_id(env, "sara"), user_id(env, "bob")
    committed = sale(sara, ids["sara"], 70_000).json()
    profit_before = legacy_money(env)["profit_ledger"]
    legacy_before = {k: v for k, v in legacy_money(env).items() if k != "profit_ledger"}

    response = mark_failed(bob, committed["id"])  # any signed-in account may record the outcome
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed" and response.json()["sim_card_id"] == ids["sara"]
    assert balance(env, ids["sara"]) == 1_000_000
    assert ledger(env)[-1] == (ids["sara"], "refund", 70_000, 1_000_000, committed["id"], bob_id)
    assert scalar(env, "SELECT reference FROM sim_ledger WHERE kind = 'refund'") == f"REFUND-{committed['reference']}"
    # Commission/profit history and the performer are untouched; legacy too.
    assert legacy_money(env)["profit_ledger"] == profit_before
    assert {k: v for k, v in legacy_money(env).items() if k != "profit_ledger"} == legacy_before
    assert scalar(env, "SELECT performed_by_user_id FROM transactions WHERE id = :id", id=committed["id"]) == sara_id

    again = mark_failed(sara, committed["id"])
    assert again.status_code == 409
    assert sara.delete(f"/api/transactions/{committed['id']}").status_code == 204
    assert sara.delete(f"/api/transactions/{committed['id']}").status_code == 204
    assert balance(env, ids["sara"]) == 1_000_000
    assert [row[1] for row in ledger(env) if row[4] == committed["id"]] == ["sale", "refund"]


def test_the_ledger_is_append_only(shop) -> None:
    env = shop["env"]
    for statement in ("UPDATE sim_ledger SET amount_cents = 1", "DELETE FROM sim_ledger"):
        with pytest.raises(Exception, match="append-only"):
            async def write(session, statement=statement):
                await session.execute(text(statement))
                await session.commit()
            env.run(write)
    assert len(ledger(env)) == 3


def test_concurrent_failure_marks_refund_once(racing) -> None:
    add_account(racing, "owner", role="admin")
    with TestClient(app) as setup:
        sign_in(setup, racing.sessions, username="owner")
        sim_id = fund_sim(setup, "mtn", 80_000)
        committed = sale(setup, sim_id, 80_000).json()
    barrier = threading.Barrier(3)
    statuses: list[int] = []

    def attempt() -> None:
        with TestClient(app) as client:
            sign_in(client, racing.sessions, username="owner")
            barrier.wait()
            statuses.append(mark_failed(client, committed["id"]).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(statuses) == [200, 409, 409]
    assert balance(racing, sim_id) == 80_000
    assert [row[1] for row in ledger(racing) if row[4] == committed["id"]] == ["sale", "refund"]


def test_a_legacy_sale_without_sim_is_never_refunded_to_a_guessed_sim(shop) -> None:
    env, admin, ids = shop["env"], shop["admin"], shop["ids"]

    async def legacy(session):
        await session.execute(text(
            "INSERT INTO transactions (id, service_id, amount_cents, status, ussd_string, reference,"
            " created_at, customer_phone) VALUES (900, 1, 5000, 'successful', '*legacy#', 'TXN-legacy-900',"
            " '2026-01-02 10:00:00', '0944123456')"))
        await session.execute(text(
            "INSERT INTO profit_ledger (transaction_id, operator_code, amount_cents, commission_cents, profit_cents,"
            " created_at) VALUES (900, 'mtn', 5000, 250, 250, '2026-01-02 10:00:00')"))
        await session.execute(text("UPDATE operator_balance SET balance_cents = 4242 WHERE operator_id = 1"))
        await session.commit()
    env.run(legacy)
    before = (legacy_money(env), {sim: balance(env, sim) for sim in ids.values()}, ledger(env))

    response = mark_failed(admin, 900)
    assert response.status_code == 409
    assert response.json()["detail"] == LEGACY_NO_SIM
    assert scalar(env, "SELECT status FROM transactions WHERE id = 900") == "successful"
    assert (legacy_money(env), {sim: balance(env, sim) for sim in ids.values()}, ledger(env)) == before
    listed = {item["id"]: item for item in admin.get("/api/transactions").json()}
    assert listed[900]["funding"] == "legacy" and listed[900]["sim_card_id"] is None


def test_archived_sim_keeps_its_history_and_receives_its_refund(shop) -> None:
    env, sara, admin, ids = shop["env"], shop["sara"], shop["admin"], shop["ids"]
    committed = sale(sara, ids["sara"], 25_000).json()
    assert sara.post(f"/api/sims/{ids['sara']}/archive").status_code == 200
    # Not usable for anything new ...
    assert sale(sara, ids["sara"], 1).json()["detail"] == SIM_UNAVAILABLE
    assert preview(sara, ids["sara"], 1).json()["detail"] == SIM_UNAVAILABLE
    assert topup_sim(sara, ids["sara"], 1).status_code == 409
    assert set_default(sara, "mtn", ids["sara"]).status_code == 400
    # ... but its history stays readable, and its sale's refund goes back to it.
    listed = {item["id"]: item for item in sara.get("/api/transactions").json()}
    assert listed[committed["id"]]["sim_card_id"] == ids["sara"]
    assert listed[committed["id"]]["sim_display_name"] == "Sara secret MTN"
    assert mark_failed(admin, committed["id"]).status_code == 200
    assert balance(env, ids["sara"]) == 1_000_000
    assert ledger(env)[-1][:3] == (ids["sara"], "refund", 25_000)
    assert sara.get(f"/api/sims/{ids['sara']}").json()["balance_cents"] == 1_000_000


def test_transaction_history_never_names_another_sellers_private_sim(shop) -> None:
    sara, bob, ids = shop["sara"], shop["bob"], shop["ids"]
    mine = sale(bob, ids["bob"], 1_000).json()
    shared = sale(bob, ids["shared"], 1_000).json()
    listed = {item["id"]: item for item in sara.get("/api/transactions").json()}
    assert listed[mine["id"]]["funding"] == "sim"
    assert listed[mine["id"]]["sim_card_id"] is None and listed[mine["id"]]["sim_display_name"] is None
    assert listed[shared["id"]]["sim_display_name"] == "Shop MTN"
    raw = sara.get("/api/transactions").text + sara.get("/transactions").text
    assert "Bob hidden MTN" not in raw
    admin_view = {item["id"]: item for item in shop["admin"].get("/api/transactions").json()}
    assert admin_view[mine["id"]]["sim_display_name"] == "Bob hidden MTN"


# ---------------------------------------------------------------------------
# 14. permissions and CSRF on every new mutation
# ---------------------------------------------------------------------------


def test_every_money_mutation_requires_csrf_and_a_session(shop) -> None:
    env, ids = shop["env"], shop["ids"]
    sara = shop["sara"]
    committed = sale(sara, ids["sara"], 1_000).json()
    before = (counts(env), {sim: balance(env, sim) for sim in ids.values()}, default_rows(env))
    calls = [
        ("POST", f"/api/sims/{ids['sara']}/topup", {"amount_cents": 100, "topup_key": new_key()}),
        ("PUT", "/api/pos/default-sim", {"operator_code": "mtn", "sim_id": ids["sara"]}),
        ("POST", "/api/transactions", {"customer_phone": "0944123456", "operator_code": "mtn",
                                       "service_code": "prepaid", "amount_cents": 100,
                                       "sim_card_id": ids["sara"], "commit_key": new_key()}),
        ("PATCH", f"/api/transactions/{committed['id']}/status", {"status": "failed"}),
    ]
    csrf = sara.headers.pop(auth.CSRF_HEADER)
    try:
        for method, url, body in calls:
            assert sara.request(method, url, json=body).status_code == 403, url
    finally:
        sara.headers[auth.CSRF_HEADER] = csrf
    with TestClient(app) as anonymous:
        for method, url, body in calls:
            assert anonymous.request(method, url, json=body).status_code == 401, url
        assert anonymous.get("/api/pos/sims").status_code == 401
    assert (counts(env), {sim: balance(env, sim) for sim in ids.values()}, default_rows(env)) == before


# ---------------------------------------------------------------------------
# 15. pages (EN/AR) and the real page scripts
# ---------------------------------------------------------------------------


def test_pos_page_offers_the_sim_selector_in_both_languages(shop) -> None:
    sara = shop["sara"]
    english = sara.get("/pos").text
    arabic = sara.get("/pos?lang=ar").text
    assert 'id="sim"' in english and 'id="pos-sim-data"' in english and 'id="sim-default"' in english
    assert "SIM card" in english and "Make default" in english
    assert "No SIM card for this company" in english
    assert 'dir="rtl"' in arabic and "الشريحة" in arabic and "اجعلها الافتراضية" in arabic
    assert "لا توجد شريحة لهذه الشركة" in arabic
    data = json.loads(re.search(r'<script type="application/json" id="pos-sim-data">(.*?)</script>',
                                english, re.S).group(1))
    assert [c["operator_code"] for c in data["companies"]] == ["mtn", "syriatel"]
    assert "Bob hidden MTN" not in english


def test_operators_page_shows_sim_balances_topups_and_legacy_credit(shop) -> None:
    sara, ids = shop["sara"], shop["ids"]
    assert set_default(sara, "mtn", ids["sara"]).status_code == 200
    english = sara.get("/operators").text
    arabic = sara.get("/operators?lang=ar").text
    assert "Legacy company credit" in english and "رصيد الشركة القديم" in arabic
    assert "Not used for new sales" in english
    assert "fetch('/api/operators/topup'" not in english  # the legacy pool is read-only
    assert f'data-sim-id="{ids["sara"]}"' in english
    assert 'data-field="sim-balance"' in english and ">10,000</span>" in english
    assert english.count('class="topup-form') >= 2 and 'data-action="default"' in english
    assert "Your default" in english and "افتراضيتك" in arabic
    assert "Top up" in english and "تعبئة" in arabic
    assert 'dir="rtl"' in arabic


def test_transactions_page_labels_legacy_and_sim_rows(shop) -> None:
    admin = shop["admin"]
    english = admin.get("/transactions").text
    arabic = admin.get("/transactions?lang=ar").text
    assert "Legacy company credit" in english and "رصيد الشركة القديم" in arabic
    assert "SIM card" in english and "الشريحة" in arabic


def _node():
    if not shutil.which("node"):
        pytest.skip("Node.js required to execute the rendered page scripts")


def _pos_run(client: TestClient, steps, **options):
    _node()
    html = client.get("/pos", params={"lang": options.get("lang", "en")}).text
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    script = next(s for s in scripts if "charge-form" in s)
    data = json.loads(re.search(r'id="pos-sim-data">(.*?)</script>', html, re.S).group(1))
    payload = {"html": html, "script": script, "steps": steps, "sims": data, **options}
    result = subprocess.run(["node", str(HARNESS)], input=json.dumps(payload), text=True,
                            capture_output=True, check=True)
    return json.loads(result.stdout)


GENERATE = [{"values": {"phone": "0944123456"}, "id": "charge-form", "event": "submit"}]


def test_pos_script_preselects_the_default_and_sends_the_chosen_sim(shop) -> None:
    sara, ids = shop["sara"], shop["ids"]
    assert set_default(sara, "mtn", ids["sara"]).status_code == 200
    result = _pos_run(sara, GENERATE + [{"id": "copy-ussd", "event": "click"}])
    assert result["elements"]["sim"]["value"] == str(ids["sara"])
    assert result["previews"][0]["body"]["sim_card_id"] == ids["sara"]
    assert [c["body"]["sim_card_id"] for c in result["commits"]] == [ids["sara"]]
    # Manual choice wins over the default and reaches preview and commit.
    manual = _pos_run(sara, [{"values": {"sim": str(ids["shared"])}, "id": "sim", "event": "change"}]
                      + GENERATE + [{"id": "copy-ussd", "event": "click"}])
    assert manual["previews"][0]["body"]["sim_card_id"] == ids["shared"]
    assert [c["body"]["sim_card_id"] for c in manual["commits"]] == [ids["shared"]]


def test_pos_script_without_a_sim_shows_the_add_sim_state_and_never_previews(shop) -> None:
    sara = shop["sara"]
    # No default saved: nothing is preselected and nothing is sent.
    result = _pos_run(sara, GENERATE)
    assert result["elements"]["sim"]["value"] == ""
    assert result["previews"] == [] and result["commits"] == []
    assert result["elements"]["register-status"]["text"] == "Select a SIM card for this company."
    # A company without any usable SIM shows the localized empty state.
    empty = _pos_run(sara, [{"values": {"operator": "syriatel"}, "id": "operator", "event": "change"}],
                     sims={"companies": [{"operator_code": "mtn", "sims": [], "default_sim_id": None},
                                         {"operator_code": "syriatel", "sims": [], "default_sim_id": None}]})
    assert empty["elements"]["sim-empty"]["hidden"] is False
    assert empty["elements"]["sim"]["disabled"] is True
    arabic = _pos_run(sara, GENERATE, lang="ar")
    assert arabic["previews"] == []


def test_pos_script_switching_company_switches_sim_choices_and_default(shop) -> None:
    sara, ids = shop["sara"], shop["ids"]
    assert set_default(sara, "syriatel", ids["syr"]).status_code == 200
    result = _pos_run(sara, [{"values": {"operator": "syriatel"}, "id": "operator", "event": "change"}])
    assert result["elements"]["sim"]["value"] == str(ids["syr"])
    assert result["elements"]["sim-empty"]["hidden"] is True
    back = _pos_run(sara, [{"values": {"operator": "syriatel"}, "id": "operator", "event": "change"},
                           {"values": {"operator": "mtn"}, "id": "operator", "event": "change"}])
    assert back["elements"]["sim"]["value"] == ""  # MTN has no default


def test_pos_script_saves_the_default_through_the_api(shop) -> None:
    sara, ids = shop["sara"], shop["ids"]
    result = _pos_run(sara, [{"values": {"sim": str(ids["shared"])}, "id": "sim", "event": "change"},
                             {"id": "sim-default", "event": "click"}])
    puts = [r for r in result["requests"] if r["method"] == "PUT"]
    assert puts == [{"url": "/api/pos/default-sim", "method": "PUT",
                     "body": {"operator_code": "mtn", "sim_id": ids["shared"]}}]


def test_pos_script_failed_copy_commits_nothing(shop) -> None:
    sara, ids = shop["sara"], shop["ids"]
    assert set_default(sara, "mtn", ids["sara"]).status_code == 200
    result = _pos_run(sara, GENERATE + [{"id": "copy-ussd", "event": "click"}],
                      clipboard="reject", execCommand="fail")
    assert result["previews"] and result["commits"] == []


def test_pos_script_reports_sim_errors_in_the_page_language(shop) -> None:
    sara, ids = shop["sara"], shop["ids"]
    assert set_default(sara, "mtn", ids["sara"]).status_code == 200
    result = _pos_run(sara, GENERATE, preview="insufficient")
    assert result["elements"]["register-status"]["text"] == "Not enough balance on this SIM card."
    arabic = _pos_run(sara, GENERATE, preview="insufficient", lang="ar")
    assert arabic["elements"]["register-status"]["text"] == "رصيد هذه الشريحة غير كافٍ."


def test_operators_topup_script_posts_whole_syp_to_the_sim(shop) -> None:
    _node()
    sara, ids = shop["sara"], shop["ids"]
    html = sara.get("/operators").text
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    steps = [{"values": {'topup:input[name="amount"]': "50000"}, "id": "topup", "event": "submit"}]
    result = subprocess.run(["node", str(MONEY_HARNESS)],
                            input=json.dumps({"script": scripts[0], "steps": steps, "simId": ids["sara"]}),
                            text=True, capture_output=True, check=True)
    output = json.loads(result.stdout)
    [post] = [r for r in output["requests"] if r["method"] == "POST"]
    assert post["url"] == f"/api/sims/{ids['sara']}/topup"
    assert post["body"]["amount_cents"] == 5_000_000
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,64}", post["body"]["topup_key"])
    assert output["elements"]['card:[data-field="sim-balance"]']["text"] == "50,000"
    # The captured request is accepted by the real API, exactly once.
    assert sara.post(post["url"], json=post["body"]).status_code == 200
    assert sara.post(post["url"], json=post["body"]).status_code == 200
    assert sara.get(f"/api/sims/{ids['sara']}").json()["balance_cents"] == 6_000_000
