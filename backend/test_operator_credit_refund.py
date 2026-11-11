"""Face-credit refund durability and failure-path regression coverage."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select, text, update

from backend.database import Operator, OperatorCreditRefund, Service, Transaction
from backend import database
from backend.test_transaction_status import (
    client, balance, create_transaction, fund, profits, raw_row, run_db, set_failed,
)


def refunds(sessions):
    async def read(session):
        rows = (await session.scalars(select(OperatorCreditRefund).order_by(OperatorCreditRefund.id))).all()
        return [(r.transaction_id, r.transaction_reference, r.operator_id, r.amount_cents) for r in rows]
    return run_db(sessions, read)


def test_existing_database_upgrade_installs_refund_audit_once(client, monkeypatch):
    _, sessions = client
    engine = sessions.kw['bind']
    async def exercise():
        async with engine.begin() as connection:
            await connection.exec_driver_sql('DROP TABLE operator_credit_refund')
        monkeypatch.setattr(database, 'engine', engine)
        monkeypatch.setattr(database, 'DATABASE_URL', engine.url)
        assert await database.upgrade_existing_database() is True
        assert await database.upgrade_existing_database() is False
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql('SELECT count(*) FROM operator_credit_refund')).scalar() == 0
            assert (await connection.exec_driver_sql("SELECT version FROM schema_migrations WHERE version='slice-2.3c-operator-credit-refund'")).scalar()
            assert (await connection.exec_driver_sql('PRAGMA integrity_check')).scalar() == 'ok'
            assert (await connection.exec_driver_sql('PRAGMA foreign_key_check')).all() == []
    asyncio.run(exercise())


@pytest.mark.parametrize('name', ['Ahmad', '   '])
def test_concurrent_duplicates_retries_and_removal_credit_once(client, name):
    http, sessions = client
    fund(http, amount_cents=100_000)
    fund(http, operator_code='syriatel', amount_cents=75_000)
    sale = create_transaction(http, amount_cents=12_345, customer_name=name)
    unrelated = create_transaction(http, operator_code='syriatel', phone='0933123456', customer_name=None)
    profit_before = profits(sessions)
    unrelated_before = raw_row(sessions, unrelated['id'])
    gate = Barrier(2)
    def fail():
        gate.wait(timeout=10)
        return set_failed(http, sale['id']).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fail(), range(2)))
    assert sorted(results) == [200, 409]
    assert balance(sessions) == 100_000
    assert balance(sessions, 'syriatel') == 70_000
    expected = refunds(sessions)
    assert len(expected) == 1 and expected[0][3] == 12_345
    for _ in range(3):
        assert set_failed(http, sale['id']).status_code == 409
        assert http.delete(f"/api/transactions/{sale['id']}").status_code == 204
    assert set_failed(http, sale['id']).status_code == 409
    assert balance(sessions) == 100_000
    assert refunds(sessions) == expected
    assert profits(sessions) == profit_before
    assert raw_row(sessions, unrelated['id']) == unrelated_before


@pytest.mark.parametrize('fault', ['balance', 'audit'])
def test_refund_write_failure_rolls_back_status_credit_and_audit(client, fault):
    http, sessions = client
    fund(http, amount_cents=100_000)
    sale = create_transaction(http)
    original = raw_row(sessions, sale['id'])
    ledger = profits(sessions)
    target = 'operator_balance' if fault == 'balance' else 'operator_credit_refund'
    action = 'UPDATE' if fault == 'balance' else 'INSERT'
    async def install(session):
        await session.execute(text(f"CREATE TRIGGER fail_refund BEFORE {action} ON {target} BEGIN SELECT RAISE(ABORT, 'injected refund failure'); END"))
        await session.commit()
    run_db(sessions, install)
    from sqlalchemy.exc import IntegrityError
    # Keep the dependency's session open: the handler itself must roll back,
    # not rely on request teardown to repair a partially applied refund.
    from backend.database import get_db
    from backend.main import app
    async def retained_failure():
        async with sessions() as session:
            async def override():
                yield session
            previous = app.dependency_overrides[get_db]
            app.dependency_overrides[get_db] = override
            try:
                with pytest.raises(IntegrityError, match='injected refund failure'):
                    set_failed(http, sale['id'])
                assert not session.in_transaction()
            finally:
                app.dependency_overrides[get_db] = previous
    asyncio.run(retained_failure())
    assert raw_row(sessions, sale['id']) == original
    assert balance(sessions) == 95_000
    assert refunds(sessions) == []
    assert profits(sessions) == ledger
    async def uninstall(session):
        await session.execute(text('DROP TRIGGER fail_refund'))
        await session.commit()
    run_db(sessions, uninstall)
    assert set_failed(http, sale['id']).status_code == 200
    assert balance(sessions) == 100_000
    assert len(refunds(sessions)) == 1


def test_refund_uses_original_ledger_operator_not_mutable_service(client):
    http, sessions = client
    fund(http, amount_cents=100_000)
    fund(http, operator_code='syriatel', amount_cents=75_000)
    sale = create_transaction(http, amount_cents=12_345)
    async def change(session):
        service_id = await session.scalar(select(Transaction.service_id).where(Transaction.id == sale['id']))
        other = await session.scalar(select(Operator.id).where(Operator.code == 'syriatel'))
        # Avoid the per-operator service-code uniqueness guard in this hostile probe.
        await session.execute(update(Service).where(Service.id == service_id).values(operator_id=other, code='moved'))
        await database.set_setting(session, 'commission_rate', '0.99')
        await session.commit()
    run_db(sessions, change)
    assert set_failed(http, sale['id']).status_code == 200
    assert balance(sessions) == 100_000
    assert balance(sessions, 'syriatel') == 75_000
    assert refunds(sessions)[0][3] == 12_345


def test_invalid_requests_have_no_accounting_effect(client):
    http, sessions = client
    fund(http)
    sale = create_transaction(http)
    before = balance(sessions), profits(sessions), refunds(sessions), raw_row(sessions, sale['id'])
    assert set_failed(http, 999999).status_code == 404
    assert http.patch(f"/api/transactions/{sale['id']}/status", json={'status': 'successful'}).status_code == 409
    assert http.delete(f"/api/transactions/{sale['id']}").status_code == 409
    assert (balance(sessions), profits(sessions), refunds(sessions), raw_row(sessions, sale['id'])) == before


@pytest.mark.parametrize('missing', ['ledger', 'balance'])
def test_missing_original_accounting_fails_closed(client, missing):
    http, sessions = client
    fund(http, amount_cents=100_000)
    sale = create_transaction(http)
    async def remove(session):
        target = 'profit_ledger' if missing == 'ledger' else 'operator_balance'
        await session.execute(text(f'DELETE FROM {target}'))
        await session.commit()
    run_db(sessions, remove)
    before = balance(sessions), profits(sessions)
    assert set_failed(http, sale['id']).status_code == 409
    assert raw_row(sessions, sale['id'])['status'] == 'successful'
    assert (balance(sessions), profits(sessions)) == before
    assert refunds(sessions) == []


@pytest.mark.parametrize('legacy', [False, True])
def test_customer_deletion_preserves_refund_audit_and_existing_history_policy(client, legacy):
    http, sessions = client
    fund(http)
    sale = create_transaction(http)
    if legacy:
        async def old_row(session):
            await session.execute(update(Transaction).where(Transaction.id == sale['id']).values(customer_phone=None))
            await session.commit()
        run_db(sessions, old_row)
    assert set_failed(http, sale['id']).status_code == 200
    before = refunds(sessions)[0]
    customer = http.get('/api/customers/lookup', params={'phone': '0944123456'}).json()
    assert http.delete(f"/api/customers/{customer['id']}").status_code == 204
    assert refunds(sessions) == [(None if legacy else sale['id'], *before[1:])]
    assert (raw_row(sessions, sale['id']) is None) == legacy
    assert balance(sessions) == 5_000_000


def test_nameless_refund_reports_keep_existing_profit_and_register_policy(client):
    http, sessions = client
    fund(http, amount_cents=100_000)
    assert http.post('/api/register/open', json={'opening_balance_cents': 0}).status_code == 200
    sale = create_transaction(http, customer_name='   ', amount_cents=12_000)
    assert http.get('/api/customers/lookup', params={'phone': '0944123456'}).status_code == 404
    assert set_failed(http, sale['id']).status_code == 200
    summary = http.get('/api/stats/summary').json()
    assert summary['totals']['volume_cents'] == 12_000
    assert summary['totals']['profit_cents'] == 600
    assert summary['by_status'][1] == {'status': 'failed', 'count': 1, 'volume_cents': 12_000}
    assert http.get('/api/register/summary').json()['cash_collected_cents'] == 0
    assert http.get('/api/transactions', params={'status': 'failed'}).json()[0]['id'] == sale['id']
    assert http.delete(f"/api/transactions/{sale['id']}").status_code == 204
    assert http.get('/api/stats/summary').json()['totals']['profit_cents'] == 0
    assert http.get('/api/register/summary').json()['cash_collected_cents'] == 0
    assert balance(sessions) == 100_000
    assert len(profits(sessions)) == len(refunds(sessions)) == 1


def test_refund_audit_uniqueness_blocks_a_second_application(client):
    http, sessions = client
    fund(http, amount_cents=100_000)
    sale = create_transaction(http)
    assert set_failed(http, sale['id']).status_code == 200
    before = refunds(sessions)
    async def reset_status(session):
        # Hostile manual reset: the durable uniqueness guard must still win.
        await session.execute(update(Transaction).where(Transaction.id == sale['id']).values(status='successful'))
        await session.commit()
    run_db(sessions, reset_status)
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        set_failed(http, sale['id'])
    assert refunds(sessions) == before
    assert balance(sessions) == 100_000
    assert raw_row(sessions, sale['id'])['status'] == 'successful'


def test_refund_schema_install_failure_rolls_back_and_can_retry(client):
    _, sessions = client
    engine = sessions.kw['bind']
    from sqlalchemy import event
    def fail_version(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT OR IGNORE INTO schema_migrations'):
            raise RuntimeError('injected version failure')
    async def exercise():
        async with engine.begin() as connection:
            await connection.exec_driver_sql('DROP TABLE operator_credit_refund')
        event.listen(engine.sync_engine, 'before_cursor_execute', fail_version)
        try:
            with pytest.raises(RuntimeError, match='injected version failure'):
                await database.migrate_operator_credit_refunds(engine)
        finally:
            event.remove(engine.sync_engine, 'before_cursor_execute', fail_version)
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name='operator_credit_refund'")).first() is None
        assert await database.migrate_operator_credit_refunds(engine) is True
        assert await database.migrate_operator_credit_refunds(engine) is False
    asyncio.run(exercise())


def test_refund_overflow_rejects_and_preserves_integer_accounting(client):
    http, sessions = client
    fund(http, amount_cents=100_000)
    sale = create_transaction(http)
    async def corrupt(session):
        await session.execute(text('UPDATE operator_balance SET balance_cents=:value'), {'value': 9223372036854775807})
        await session.commit()
    run_db(sessions, corrupt)
    original = raw_row(sessions, sale['id'])
    ledger = profits(sessions)
    assert set_failed(http, sale['id']).status_code == 409
    async def stored(session):
        return (await session.execute(text('SELECT balance_cents, typeof(balance_cents) FROM operator_balance WHERE operator_id=1'))).one()
    assert tuple(run_db(sessions, stored)) == (9223372036854775807, 'integer')
    assert raw_row(sessions, sale['id']) == original
    assert profits(sessions) == ledger
    assert refunds(sessions) == []


@pytest.mark.parametrize('column', ['amount_cents', 'balance_cents'])
@pytest.mark.parametrize('value', [-1, 1.5, 'invalid'])
def test_malformed_stored_refund_money_fails_closed(client, column, value):
    http, sessions = client
    fund(http, amount_cents=100_000)
    sale = create_transaction(http)
    async def corrupt(session):
        # Model constraints normally reject negative transaction amounts; simulate
        # an imported/hand-edited SQLite file without weakening production checks.
        await session.execute(text('PRAGMA ignore_check_constraints=ON'))
        table = 'transactions' if column == 'amount_cents' else 'operator_balance'
        await session.execute(text(f'UPDATE {table} SET {column}=:value'), {'value': value})
        await session.commit()
    run_db(sessions, corrupt)
    async def snapshot(session):
        return (await session.execute(text('SELECT amount_cents, typeof(amount_cents), status FROM transactions'))).all(), (await session.execute(text('SELECT balance_cents, typeof(balance_cents) FROM operator_balance'))).all()
    before = run_db(sessions, snapshot)
    assert set_failed(http, sale['id']).status_code == 409
    assert run_db(sessions, snapshot) == before
    assert refunds(sessions) == []


@pytest.mark.parametrize('defect', ['columns', 'type', 'nullable', 'fk', 'ondelete', 'unique', 'check'])
def test_incompatible_refund_schema_rejected_without_mutation(client, defect):
    _, sessions = client
    engine = sessions.kw['bind']
    from sqlalchemy.schema import CreateTable
    from backend.migrations import SchemaUpgradeError
    ddl = str(CreateTable(OperatorCreditRefund.__table__).compile(engine.sync_engine))
    replacements = {
        'type': ('amount_cents INTEGER', 'amount_cents TEXT'),
        'nullable': ('amount_cents INTEGER NOT NULL', 'amount_cents INTEGER'),
        'fk': ('REFERENCES operators (id)', 'REFERENCES customers (id)'),
        'ondelete': ('ON DELETE SET NULL', 'ON DELETE CASCADE'),
        'unique': ('UNIQUE (transaction_id)', 'UNIQUE (transaction_id, operator_id)'),
        'check': ('amount_cents >= 0', 'amount_cents >= -1'),
    }
    if defect == 'columns':
        ddl = 'CREATE TABLE operator_credit_refund(id INTEGER PRIMARY KEY)'
    else:
        old, new = replacements[defect]
        assert old in ddl
        ddl = ddl.replace(old, new)
    async def exercise():
        async with engine.begin() as connection:
            await connection.exec_driver_sql('DROP TABLE operator_credit_refund')
            await connection.exec_driver_sql(ddl)
        async with engine.connect() as connection:
            before = (await connection.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY name')).all()
        with pytest.raises(SchemaUpgradeError, match='operator_credit_refund'):
            await database.migrate_operator_credit_refunds(engine)
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY name')).all() == before
    asyncio.run(exercise())


@pytest.mark.parametrize('entry', ['init', 'desktop', 'startup'])
def test_startup_rejects_bad_audit_before_any_mutation(client, monkeypatch, entry):
    _, sessions = client
    engine = sessions.kw['bind']
    from backend import desktop
    from backend.migrations import SchemaUpgradeError
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'DATABASE_URL', engine.url)
    monkeypatch.setattr(desktop, 'engine', engine)
    monkeypatch.setattr(desktop, 'SessionLocal', sessions)
    async def exercise():
        async with engine.begin() as connection:
            await connection.exec_driver_sql('DROP TABLE operator_credit_refund')
            await connection.exec_driver_sql('CREATE TABLE operator_credit_refund(id INTEGER PRIMARY KEY)')
            await connection.exec_driver_sql('DELETE FROM settings')
            await connection.exec_driver_sql('DROP TABLE currency_denomination')
        async with engine.connect() as connection:
            before = (await connection.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY name')).all()
        action = {'init': database.init_db, 'desktop': desktop._seed_database, 'startup': database.upgrade_existing_database}[entry]
        with pytest.raises(SchemaUpgradeError, match='operator_credit_refund'):
            await action()
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY name')).all() == before
            assert (await connection.exec_driver_sql('SELECT count(*) FROM settings')).scalar() == 0
    asyncio.run(exercise())


def test_desktop_fresh_database_records_refund_schema_version(client, monkeypatch):
    _, sessions = client
    engine = sessions.kw['bind']
    from backend import desktop
    monkeypatch.setattr(desktop, 'engine', engine)
    monkeypatch.setattr(desktop, 'SessionLocal', sessions)
    async def exercise():
        async with engine.begin() as connection:
            await connection.run_sync(database.Base.metadata.drop_all)
            await connection.exec_driver_sql('DROP TABLE IF EXISTS schema_migrations')
        await desktop._seed_database()
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql("SELECT version FROM schema_migrations WHERE version='slice-2.3c-operator-credit-refund'")).scalar()
            await connection.run_sync(database._validate_refund_schema)
        assert await database.migrate_operator_credit_refunds(engine) is False
    asyncio.run(exercise())
