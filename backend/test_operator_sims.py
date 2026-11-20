"""v1.0.4 Slice 4: operator SIM cards (identity, visibility, archive) and the
Operators page.

Scope of this slice (see README "Operators and SIM cards"):

* MTN and Syriatel stay the two *company* configurations (``operators`` with
  their services/USSD settings). A SIM card is a separate record
  (``sim_cards``) that belongs to exactly one company.
* SIM cards carry no money yet. Sales, top-ups and refunds keep using the
  per-company *shop credit* (``operator_balance``); per-SIM balances, per-SIM
  top-ups, per-user defaults and POS selection are the next slice (4.1). The
  legacy mapping in this slice is therefore the identity: every balance, top-up,
  refund and transaction stays attached to its company, nothing is moved.
* Visibility: admins see every SIM; a seller sees active shared SIMs and their
  own private SIMs (active or archived). A SIM the caller may not see answers
  404 on every route, so its existence is not revealed.

Every scenario runs against a disposable database under ``tmp_path``.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import sqlite3
import subprocess
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from backend import auth, database, seed
from backend.auth_test_support import sign_in
from backend.sim_test_support import sim_balance, topup_sim
from backend.database import Operator, OperatorBalance, Service, User
from backend.main import app
from backend.migrations import SchemaUpgradeError
from backend.test_auth import env, fresh  # noqa: F401 - isolated DB fixtures
from backend.test_transaction_attribution import PRESERVED_TABLES, _build_v103

SIM_MARKER = "v1.0.4-sim-cards"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def rows(env, statement: str, **params) -> list[tuple]:
    async def run(session):
        return [tuple(row) for row in (await session.execute(text(statement), params)).all()]
    return env.run(run)


def sim_rows(env) -> list[tuple]:
    return rows(env, "SELECT id, operator_id, display_name, phone, owner_user_id, is_shared,"
                     " archived_at IS NOT NULL FROM sim_cards ORDER BY id")


def user_id(env, username: str) -> int:
    async def read(session):
        return await session.scalar(select(User.id).where(User.username == username))
    return env.run(read)


def add_account(env, username: str, role: str = "seller", active: bool = True) -> int:
    async def add(session):
        user = User(username=username, password_hash=auth.hash_password("x" * 12), role=role,
                    is_active=active, must_change_password=False)
        session.add(user)
        await session.commit()
        return user.id
    return env.run(add)


@pytest.fixture
def people(fresh):
    """Signed-in browsers: ``people("sara", "seller")`` -> TestClient.

    The shop is set up (an active admin exists); otherwise every request would
    land on the Slice 3.1 fail-closed "locked" page."""
    add_account(fresh, "owner", role="admin")
    with ExitStack() as stack:
        cache: dict[str, TestClient] = {}

        def open_as(username: str, role: str = "seller") -> TestClient:
            if username not in cache:
                client = stack.enter_context(TestClient(app))
                cache[username] = sign_in(client, fresh.sessions, role=role, username=username)
            return cache[username]

        yield open_as


def new_sim(client: TestClient, **overrides):
    body = {"operator_code": "mtn", "display_name": "Counter MTN", "phone": "0944000001",
            "visibility": "private"}
    body.update(overrides)
    return client.post("/api/sims", json=body)


def created(client: TestClient, **overrides) -> dict:
    response = new_sim(client, **overrides)
    assert response.status_code == 201, response.text
    return response.json()


def listed_ids(client: TestClient, archived: bool = False) -> set[int]:
    params = {"include_archived": "true"} if archived else {}
    response = client.get("/api/sims", params=params)
    assert response.status_code == 200, response.text
    return {sim["id"] for sim in response.json()}


def file_digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_all_startups() -> None:
    from backend import desktop

    asyncio.run(seed.main())
    asyncio.run(desktop._seed_database())
    asyncio.run(database.upgrade_existing_database())
    with TestClient(app):  # the web startup hook
        pass


def markers(env) -> set[str]:
    return {row[0] for row in rows(env, "SELECT version FROM schema_migrations")}


# ---------------------------------------------------------------------------
# 1. fresh install: companies configured, no SIM cards, setup still required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["source", "desktop"])
def test_fresh_install_has_both_companies_but_no_sim_cards(env, entry) -> None:
    from backend import desktop

    asyncio.run(seed.main() if entry == "source" else desktop._seed_database())

    async def companies(session):
        operators = (await session.scalars(select(Operator).order_by(Operator.code))).all()
        services = (await session.scalars(select(Service))).all()
        return [o.code for o in operators], {s.operator_id for s in services}
    codes, with_services = env.run(companies)
    assert codes == ["mtn", "syriatel"]
    assert len(with_services) == 2
    assert sim_rows(env) == []
    assert SIM_MARKER in markers(env)
    # First-run auth is unchanged: no account, setup first, APIs closed.
    assert rows(env, "SELECT count(*) FROM users") == [(0,)]
    with TestClient(app) as browser:
        assert browser.get("/operators", follow_redirects=False).headers["location"] == "/setup"
        assert browser.get("/api/sims").status_code == 401


# ---------------------------------------------------------------------------
# 2 + 3. migrations: preserved money, identity mapping, idempotent, fail closed
# ---------------------------------------------------------------------------


def _add_legacy_money(env) -> None:
    """Top-ups and a refund audit on top of the v1.0.3 history fixture."""
    async def add(session):
        await session.execute(text(
            "INSERT INTO operator_topup (id, operator_id, amount_cents, reference, created_at) VALUES"
            " (1, 1, 100000, 'TOPUP-legacy-1', '2026-01-01 09:00:00'),"
            " (2, 2, 30000, 'TOPUP-legacy-2', '2026-01-01 09:30:00')"
        ))
        await session.execute(text(
            "INSERT INTO operator_credit_refund (id, transaction_id, transaction_reference, operator_id,"
            " amount_cents, created_at) VALUES (1, 3, 'TXN-legacy-c', 1, 700, '2026-01-04 13:00:00')"
        ))
        await session.commit()
    env.run(add)


def _money_snapshot(env) -> dict:
    snapshot = {table: rows(env, f"SELECT * FROM {table} ORDER BY 1") for table in PRESERVED_TABLES}
    snapshot["transactions"] = rows(
        env, "SELECT id, customer_id, service_id, amount_cents, status, reference, idempotency_key,"
             " deleted_at, legacy_status, customer_phone FROM transactions ORDER BY id")
    return snapshot


def _dump(path: str) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def test_v103_upgrade_preserves_every_money_row_and_creates_no_sim(env) -> None:
    asyncio.run(_build_v103(env.engine, env.sessions))
    _add_legacy_money(env)
    tables = {row[0] for row in rows(env, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sim_cards" not in tables  # a faithful v1.0.3 file
    before = _money_snapshot(env)
    assert before["operator_balance"] and before["operator_topup"] and before["operator_credit_refund"]

    run_all_startups()

    assert _money_snapshot(env) == before
    assert sim_rows(env) == []
    assert SIM_MARKER in markers(env)
    # Identity mapping, queryable: each legacy balance/top-up/refund/sale still
    # resolves to its company, and the shop credit reads exactly as before.
    assert rows(env, "SELECT o.code, b.balance_cents FROM operator_balance b"
                     " JOIN operators o ON o.id = b.operator_id ORDER BY o.code") == [
        ("mtn", 123456), ("syriatel", 123456)]
    assert rows(env, "SELECT t.reference, o.code FROM operator_topup t"
                     " JOIN operators o ON o.id = t.operator_id ORDER BY t.id") == [
        ("TOPUP-legacy-1", "mtn"), ("TOPUP-legacy-2", "syriatel")]
    assert rows(env, "SELECT r.transaction_reference, o.code FROM operator_credit_refund r"
                     " JOIN operators o ON o.id = r.operator_id") == [("TXN-legacy-c", "mtn")]
    assert rows(env, "SELECT t.reference, o.code FROM transactions t JOIN services s ON s.id = t.service_id"
                     " JOIN operators o ON o.id = s.operator_id ORDER BY t.id") == [
        ("TXN-legacy-a", "mtn"), ("TXN-legacy-b", "mtn"), ("TXN-legacy-c", "mtn")]

    # Idempotent: a second round of every startup path changes nothing at all.
    first = _dump(env.engine.url.database)
    run_all_startups()
    assert _dump(env.engine.url.database) == first


SIM_FK_CLAUSE = ", \n\tFOREIGN KEY(sim_card_id) REFERENCES sim_cards (id) ON DELETE RESTRICT"


def _remove_sim_schema(path: str) -> None:
    """Strip everything Slice 4/4.1 added from a still-empty database file.

    Drops the SIM card and SIM balance tables (and the ledger's triggers),
    their markers, and rebuilds the empty ``transactions`` table without the
    Slice 4.1 ``sim_card_id`` column, leaving the exact Slice 3.1 shape.
    """
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM transactions").fetchone() == (0,)
        connection.execute("PRAGMA foreign_keys=OFF")
        with connection:
            for trigger in ("sim_ledger_no_update", "sim_ledger_no_delete"):
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for table in ("sim_defaults", "sim_ledger", "sim_balances", "sim_cards"):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.execute("DELETE FROM schema_migrations WHERE version IN (?, ?)",
                               (SIM_MARKER, database.SIM_BALANCE_SCHEMA_VERSION))
            ddl = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='transactions'").fetchone()[0]
            # The dump is upgraded by ADD COLUMN (inline FK); create_all
            # declares a table-level FK. Both represent the same old schema.
            inline = ", sim_card_id INTEGER REFERENCES sim_cards (id) ON DELETE RESTRICT"
            assert inline in ddl or ("\tsim_card_id INTEGER, \n" in ddl and SIM_FK_CLAUSE in ddl), ddl
            ddl = ddl.replace(inline, "").replace("\tsim_card_id INTEGER, \n", "").replace(SIM_FK_CLAUSE, "")
            indexes = [sql for (sql,) in connection.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name='transactions' AND type IN ('index', 'trigger')"
                " AND sql IS NOT NULL")]
            assert not [sql for sql in indexes if "sim_card_id" in sql]
            connection.execute("DROP TABLE transactions")
            connection.execute(ddl)
            for sql in indexes:
                connection.execute(sql)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert "sim_card_id" not in [row[1] for row in connection.execute("PRAGMA table_info(transactions)")]
    finally:
        connection.close()


def _build_v104_slice31(env) -> None:
    """A v1.0.4 (Slice 3.1) install: auth, performer column, no sim_cards."""
    # Load the actual historical schema, including its retired directory
    # linkage, instead of inventing old columns on current application models.
    connection = sqlite3.connect(env.engine.url.database)
    try:
        connection.executescript(Path(__file__).with_name("fixtures").joinpath("v104_slice4.sql").read_text())
    finally:
        connection.close()
    asyncio.run(seed.main())  # pre-Slice-4 code path also ran init_db; SIM schema removed below
    _remove_sim_schema(env.engine.url.database)
    owner = add_account(env, "owner", role="admin")
    async def history(session):
        await session.execute(text("UPDATE operator_balance SET balance_cents = 777000 WHERE operator_id = 2"))
        await session.execute(text(
            "INSERT INTO transactions (id, customer_id, service_id, amount_cents, status, ussd_string, reference,"
            " created_at, customer_phone, performed_by_user_id) VALUES"
            " (1, NULL, 3, 2500, 'successful', '*x#', 'TXN-v104-a', '2026-10-01 10:00:00', '0944123456', :owner)"
        ), {"owner": owner})
        await session.execute(text(
            "INSERT INTO profit_ledger (transaction_id, operator_code, amount_cents, commission_cents, profit_cents,"
            " created_at) VALUES (1, 'syriatel', 2500, 125, 125, '2026-10-01 10:00:00')"))
        await session.commit()
    env.run(history)


def test_v104_upgrade_keeps_accounts_history_and_attribution(env) -> None:
    _build_v104_slice31(env)
    before = _money_snapshot(env)
    accounts = rows(env, "SELECT id, username, password_hash, role, is_active FROM users ORDER BY id")
    performers = rows(env, "SELECT id, performed_by_user_id FROM transactions ORDER BY id")

    run_all_startups()

    assert _money_snapshot(env) == before
    assert rows(env, "SELECT id, username, password_hash, role, is_active FROM users ORDER BY id") == accounts
    assert rows(env, "SELECT id, performed_by_user_id FROM transactions ORDER BY id") == performers
    assert sim_rows(env) == [] and SIM_MARKER in markers(env)


@pytest.mark.parametrize("entry", ["web-upgrade", "source", "desktop"])
def test_incompatible_sim_table_fails_closed_without_touching_the_file(env, entry) -> None:
    from backend import desktop

    _build_v104_slice31(env)
    async def plant(session):
        # Same name, weaker shape: no visibility check, no foreign keys.
        await session.execute(text(
            "CREATE TABLE sim_cards (id INTEGER PRIMARY KEY, operator_id INTEGER, display_name TEXT,"
            " phone TEXT, owner_user_id INTEGER, is_shared BOOLEAN, archived_at DATETIME,"
            " created_by_user_id INTEGER, created_at DATETIME, updated_at DATETIME)"))
        await session.commit()
    env.run(plant)
    asyncio.run(env.engine.dispose())
    path = env.engine.url.database
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


# ---------------------------------------------------------------------------
# 4 + 5. who may create what
# ---------------------------------------------------------------------------


def test_seller_creates_private_sim_for_self_and_shared_sim(people, fresh) -> None:
    sara = people("sara")
    sara_id = user_id(fresh, "sara")

    private = created(sara, display_name="Sara MTN", phone="0944 000 001")
    assert private["visibility"] == "private"
    assert private["owner_user_id"] == sara_id and private["owner_username"] == "sara"
    assert private["phone"] == "0944000001"  # normalised like every phone
    assert private["operator_code"] == "mtn" and private["status"] == "active"

    explicit_self = created(sara, display_name="Sara Syriatel", operator_code="syriatel",
                            phone="0933000002", owner_user_id=sara_id)
    assert explicit_self["owner_user_id"] == sara_id

    shared = created(sara, display_name="Shop MTN", phone="0944000003", visibility="shared")
    assert shared["visibility"] == "shared" and shared["owner_user_id"] is None
    assert len(sim_rows(fresh)) == 3


def test_seller_cannot_assign_a_private_sim_to_someone_else(people, fresh) -> None:
    sara = people("sara")
    omar_id = add_account(fresh, "omar")
    admin_id = add_account(fresh, "boss", role="admin")
    for owner in (omar_id, admin_id):
        response = new_sim(sara, owner_user_id=owner)
        assert response.status_code == 403
    assert new_sim(sara, visibility="shared", owner_user_id=user_id(fresh, "sara")).status_code == 400
    assert sim_rows(fresh) == []


def test_admin_assigns_private_sims_and_creates_shared_ones(people, fresh) -> None:
    admin = people("boss", "admin")
    omar_id = add_account(fresh, "omar")

    assigned = created(admin, owner_user_id=omar_id, display_name="Omar MTN")
    assert assigned["owner_user_id"] == omar_id and assigned["visibility"] == "private"
    shared = created(admin, visibility="shared", operator_code="syriatel", phone="0933111222")
    assert shared["owner_user_id"] is None

    # A private SIM needs an owner, and the owner must be an active account.
    assert new_sim(admin).status_code == 400
    retired = add_account(fresh, "retired", active=False)
    for bad_owner in (retired, 999_999):
        assert new_sim(admin, owner_user_id=bad_owner).status_code == 400
    assert len(sim_rows(fresh)) == 2


@pytest.mark.parametrize("overrides,status", [
    ({"phone": "12345"}, 422),
    ({"phone": "0911111111"}, 422),
    ({"display_name": "   "}, 422),
    ({"display_name": "x" * 81}, 422),
    ({"operator_code": "zain"}, 400),
    ({"visibility": "global"}, 422),
    ({"balance_cents": 5000}, 422),  # no money fields, and unknown fields are refused
    ({"created_by_user_id": 1}, 422),
])
def test_invalid_sim_details_are_rejected(people, fresh, overrides, status) -> None:
    sara = people("sara")
    assert new_sim(sara, **overrides).status_code == status
    assert sim_rows(fresh) == []


def test_sim_writes_require_the_csrf_token(people, fresh) -> None:
    sara = people("sara")
    sim = created(sara)
    token = sara.headers.pop(auth.CSRF_HEADER)
    try:
        assert new_sim(sara, phone="0944000009").status_code == 403
        assert sara.patch(f"/api/sims/{sim['id']}", json={"display_name": "Changed"}).status_code == 403
        assert sara.post(f"/api/sims/{sim['id']}/archive").status_code == 403
        sara.headers[auth.CSRF_HEADER] = "forged-" + token[7:]
        assert sara.post(f"/api/sims/{sim['id']}/archive").status_code == 403
    finally:
        sara.headers[auth.CSRF_HEADER] = token
    assert sim_rows(fresh) == [(sim["id"], 1, "Counter MTN", "0944000001", user_id(fresh, "sara"), 0, 0)]


# ---------------------------------------------------------------------------
# 6. private SIMs are invisible to other sellers, everywhere
# ---------------------------------------------------------------------------


def test_private_sim_is_invisible_to_other_sellers(people, fresh) -> None:
    sara, omar, admin = people("sara"), people("omar"), people("boss", "admin")
    secret = created(sara, display_name="Sara Private Line", phone="0955123987")
    shared = created(admin, visibility="shared", display_name="Front Desk", phone="0944777000")
    sim_id = secret["id"]

    assert listed_ids(omar, archived=True) == {shared["id"]}
    assert listed_ids(sara) == {sim_id, shared["id"]}
    assert listed_ids(admin) == {sim_id, shared["id"]}

    # Direct object access answers 404, exactly like an id that does not exist.
    missing = omar.get("/api/sims/999999")
    for response in (
        omar.get(f"/api/sims/{sim_id}"),
        omar.patch(f"/api/sims/{sim_id}", json={"display_name": "Mine now"}),
        omar.post(f"/api/sims/{sim_id}/archive"),
        omar.post(f"/api/sims/{sim_id}/restore"),
    ):
        assert response.status_code == 404
        assert response.json() == missing.json()
    assert admin.get(f"/api/sims/{sim_id}").json()["phone"] == "0955123987"

    for lang in ("en", "ar"):
        page = omar.get("/operators", params={"lang": lang}).text
        assert "0955123987" not in page and "Sara Private Line" not in page
        assert "Front Desk" in page
    assert "Sara Private Line" in admin.get("/operators").text
    assert "Sara Private Line" in sara.get("/operators").text
    # Other read paths do not mention SIM cards at all.
    assert "0955123987" not in omar.get("/api/search", params={"q": "0955"}).text
    assert sim_rows(fresh)[0][2] == "Sara Private Line"


def test_archived_shared_sims_are_admin_only_history(people, fresh) -> None:
    sara, admin = people("sara"), people("boss", "admin")
    shared = created(admin, visibility="shared", display_name="Old Shop Line")
    assert admin.post(f"/api/sims/{shared['id']}/archive").status_code == 200
    assert listed_ids(sara, archived=True) == set()
    assert sara.get(f"/api/sims/{shared['id']}").status_code == 404
    assert listed_ids(admin, archived=True) == {shared["id"]}
    assert listed_ids(admin) == set()  # archived SIMs are left out unless asked for


# ---------------------------------------------------------------------------
# 7 + 8. edit / archive / restore rules
# ---------------------------------------------------------------------------


def test_owner_and_admin_manage_a_private_sim_other_sellers_cannot(people, fresh) -> None:
    sara, omar, admin = people("sara"), people("omar"), people("boss", "admin")
    sim = created(sara)
    url = f"/api/sims/{sim['id']}"

    edited = sara.patch(url, json={"display_name": "Sara's line", "phone": "0944000111"})
    assert edited.status_code == 200
    assert edited.json()["display_name"] == "Sara's line" and edited.json()["phone"] == "0944000111"
    # Only an admin decides who a SIM belongs to or who may use it.
    for body in ({"owner_user_id": user_id(fresh, "omar")}, {"visibility": "shared"},
                 {"owner_user_id": user_id(fresh, "sara")}):
        assert sara.patch(url, json=body).status_code == 403
    assert sara.patch(url, json={"operator_code": "syriatel"}).status_code == 422  # company is fixed

    assert omar.patch(url, json={"display_name": "x"}).status_code == 404
    assert admin.patch(url, json={"display_name": "Checked by admin"}).status_code == 200

    archived = sara.post(f"{url}/archive")
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    assert sara.post(f"{url}/archive").json()["status"] == "archived"  # repeat is safe
    assert sara.patch(url, json={"display_name": "while archived"}).status_code == 409
    assert listed_ids(sara) == set() and listed_ids(sara, archived=True) == {sim["id"]}
    assert omar.post(f"{url}/restore").status_code == 404

    restored = sara.post(f"{url}/restore")
    assert restored.status_code == 200 and restored.json()["status"] == "active"
    assert admin.post(f"{url}/archive").status_code == 200
    assert admin.post(f"{url}/restore").json()["status"] == "active"
    # The record keeps its identity throughout.
    assert sim_rows(fresh) == [(sim["id"], 1, "Checked by admin", "0944000111", user_id(fresh, "sara"), 0, 0)]


def test_admin_reassigns_and_changes_visibility(people, fresh) -> None:
    sara, omar, admin = people("sara"), people("omar"), people("boss", "admin")
    sim = created(sara)
    url = f"/api/sims/{sim['id']}"
    omar_id = user_id(fresh, "omar")

    moved = admin.patch(url, json={"owner_user_id": omar_id})
    assert moved.status_code == 200 and moved.json()["owner_username"] == "omar"
    assert listed_ids(sara, archived=True) == set() and listed_ids(omar) == {sim["id"]}

    shared = admin.patch(url, json={"visibility": "shared"})
    assert shared.json()["visibility"] == "shared" and shared.json()["owner_user_id"] is None
    assert admin.patch(url, json={"visibility": "private"}).status_code == 400  # owner required
    back = admin.patch(url, json={"visibility": "private", "owner_user_id": user_id(fresh, "sara")})
    assert back.status_code == 200 and back.json()["owner_username"] == "sara"
    retired = add_account(fresh, "retired", active=False)
    assert admin.patch(url, json={"owner_user_id": retired}).status_code == 400


def test_shared_sim_identity_is_admin_only(people, fresh) -> None:
    sara, admin = people("sara"), people("boss", "admin")
    # Even the seller who created a shared SIM cannot change it afterwards.
    shared = created(sara, visibility="shared", display_name="Shop line")
    url = f"/api/sims/{shared['id']}"

    assert sara.get(url).status_code == 200  # everyone may view an active shared SIM
    view = sara.get(url).json()
    assert view["can_edit"] is False and view["can_archive"] is False
    assert sara.patch(url, json={"display_name": "Mine"}).status_code == 403
    assert sara.post(f"{url}/archive").status_code == 403
    assert admin.patch(url, json={"display_name": "Shop line 1"}).status_code == 200
    assert admin.post(f"{url}/archive").status_code == 200
    assert sara.post(f"{url}/restore").status_code == 404  # archived shared: not visible
    assert admin.post(f"{url}/restore").json()["status"] == "active"
    assert sim_rows(fresh)[0][2] == "Shop line 1"


def test_sims_are_never_deleted(people, fresh) -> None:
    sara, admin = people("sara"), people("boss", "admin")
    sim = created(sara)
    for client in (sara, admin):
        assert client.delete(f"/api/sims/{sim['id']}").status_code == 405
    assert len(sim_rows(fresh)) == 1


def test_sim_owner_link_survives_owner_deactivation(people, fresh) -> None:
    admin = people("boss", "admin")
    omar_id = add_account(fresh, "omar")
    sim = created(admin, owner_user_id=omar_id)
    assert admin.patch(f"/api/users/{omar_id}", json={"is_active": False}).status_code == 200
    kept = admin.get(f"/api/sims/{sim['id']}").json()
    assert kept["owner_username"] == "omar" and kept["status"] == "active"


# ---------------------------------------------------------------------------
# boundary of this slice: money still flows through the company shop credit
# ---------------------------------------------------------------------------


def test_sales_use_the_selected_sim_and_never_the_company_shop_credit(people, fresh) -> None:
    """Slice 4.1: the legacy company credit is frozen; a sale debits its SIM card."""
    sara = people("sara")
    sim = created(sara)
    async def balances(session):
        return dict((await session.execute(
            select(Operator.code, OperatorBalance.balance_cents).join(Operator, Operator.id == OperatorBalance.operator_id)
        )).all())
    legacy = sara.post("/api/operators/topup", json={"operator_code": "mtn", "amount_cents": 1_000_000})
    assert legacy.status_code == 409
    assert fresh.run(balances) == {"mtn": 0, "syriatel": 0}
    assert topup_sim(sara, sim["id"], 1_000_000).status_code == 200
    sale = sara.post("/api/transactions", json={
        "customer_phone": "0944123456", "operator_code": "mtn", "service_code": "prepaid",
        "amount_cents": 50_000, "commit_key": "slice4-boundary-key-0001", "sim_card_id": sim["id"]})
    assert sale.status_code == 200, sale.text
    assert sim_balance(sara, sim["id"]) == 950_000
    assert fresh.run(balances) == {"mtn": 0, "syriatel": 0}
    # Archiving moves no money anywhere.
    assert sara.post(f"/api/sims/{sim['id']}/archive").status_code == 200
    assert fresh.run(balances) == {"mtn": 0, "syriatel": 0}
    assert rows(fresh, f"SELECT balance_cents FROM sim_balances WHERE sim_card_id = {int(sim['id'])}") == [(950_000,)]
    # The SIM directory itself still carries no money columns (balances live apart).
    columns = {row[1] for row in rows(fresh, "PRAGMA table_info(sim_cards)")}
    assert not any("balance" in column or "cents" in column for column in columns)


# ---------------------------------------------------------------------------
# 15. the Operators page
# ---------------------------------------------------------------------------


def test_operators_page_renders_in_both_languages_mtn_first(people, fresh) -> None:
    sara = people("sara")
    english = sara.get("/operators")
    arabic = sara.get("/operators", params={"lang": "ar"})
    assert english.status_code == arabic.status_code == 200
    assert '<html lang="ar" dir="rtl">' in arabic.text
    for page, words in (
        # Slice 4.1: the old shop credit is shown as the legacy company credit.
        (english.text, ("Operators", "SIM cards", "Add SIM card", "Legacy company credit", "No SIM cards yet",
                        "Remaining balance", "Last top-up")),
        (arabic.text, ("المشغّلون", "شرائح SIM", "إضافة شريحة", "رصيد الشركة القديم", "لا توجد شرائح بعد",
                       "الرصيد المتبقي", "آخر تعبئة")),
    ):
        for word in words:
            assert word in page, word
        assert page.index('data-company="mtn"') < page.index('data-company="syriatel"')
        # Both legacy credit cards are shown, read-only: no legacy top-up any more.
        assert 'data-operator-card="mtn"' in page and 'data-operator-card="syriatel"' in page
        assert "fetch('/api/operators/topup'" not in page
    # Navigation: a normal item, highlighted on this page; Top-up is gone.
    assert re.search(r'<a href="/operators"[^>]*aria-current="page"[^>]*>Operators</a>', english.text)
    assert 'href="/topup"' not in english.text
    assert re.search(r'<a href="/operators"[^>]*aria-current="page"[^>]*>المشغّلون</a>', arabic.text)


def test_old_topup_address_leads_to_operators(people) -> None:
    sara = people("sara")
    for path, target in (("/topup", "/operators"), ("/topup?lang=ar", "/operators?lang=ar")):
        response = sara.get(path, follow_redirects=False)
        assert response.status_code in (301, 302, 303, 307, 308)
        assert response.headers["location"] == target
    with TestClient(app) as anonymous:
        assert anonymous.get("/operators", follow_redirects=False).headers["location"].startswith("/login")


def test_page_controls_follow_permissions(people, fresh) -> None:
    sara, admin = people("sara"), people("boss", "admin")
    add_account(fresh, "omar")
    add_account(fresh, "retired", active=False)
    own = created(sara, display_name="Sara Line")
    shared = created(admin, visibility="shared", display_name="Shop Line", phone="0944555666")
    old = created(sara, display_name="Sara Old", phone="0944000222")
    sara.post(f"/api/sims/{old['id']}/archive")

    page = sara.get("/operators").text
    own_row = re.search(rf'<li[^>]*data-sim-id="{own["id"]}".*?</li>', page, re.S).group(0)
    shared_row = re.search(rf'<li[^>]*data-sim-id="{shared["id"]}".*?</li>', page, re.S).group(0)
    old_row = re.search(rf'<li[^>]*data-sim-id="{old["id"]}".*?</li>', page, re.S).group(0)
    assert 'data-action="edit"' in own_row and 'data-action="archive"' in own_row
    assert 'data-action="edit"' not in shared_row and 'data-action="archive"' not in shared_row
    assert 'data-action="restore"' in old_row
    assert "Shared" in shared_row and "Private" in own_row and "Archived" in old_row
    assert 'name="owner_user_id"' not in page  # sellers only create for themselves

    admin_page = admin.get("/operators").text
    owner_select = re.search(r'<select[^>]*name="owner_user_id".*?</select>', admin_page, re.S).group(0)
    assert ">omar<" in owner_select and ">sara<" in owner_select
    assert "retired" not in owner_select  # inactive accounts cannot receive a SIM
    shared_admin_row = re.search(rf'<li[^>]*data-sim-id="{shared["id"]}".*?</li>', admin_page, re.S).group(0)
    assert 'data-action="edit"' in shared_admin_row and 'data-action="archive"' in shared_admin_row
    assert "sara" in re.search(rf'<li[^>]*data-sim-id="{own["id"]}".*?</li>', admin_page, re.S).group(0)


def test_operators_page_scripts_are_valid_javascript(people, tmp_path) -> None:
    if not shutil.which("node"):
        pytest.skip("Node.js required")
    page = people("boss", "admin").get("/operators", params={"lang": "ar"}).text
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert len(scripts) == 3  # top-up, SIM cards, then the shared base search script
    # The first inline script is the per-SIM top-up handler the money form
    # harness drives (test_money_boundaries); the legacy credit is read-only.
    assert "'/api/sims/' + encodeURIComponent(form.dataset.simId) + '/topup'" in scripts[0]
    assert not any("/api/operators/topup" in script for script in scripts)
    assert "send('POST', '/api/sims', body)" in scripts[1] and "/api/pos/default-sim" in scripts[1]
    for index, script in enumerate(scripts):
        path = tmp_path / f"script{index}.js"
        path.write_text(script)
        subprocess.run(["node", "--check", str(path)], check=True, capture_output=True)
