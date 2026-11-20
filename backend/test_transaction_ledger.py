"""Combined history is a read-only projection, not another accounting path."""
import asyncio
from contextlib import aclosing
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.database import (get_db, Operator, OperatorTopup, SimLedger,
                              SimCard, SimBalance, Transaction, OperatorBalance, ProfitLedger)
from backend.main import app
from backend.test_transactions_list import client, transactions
from backend.sim_test_support import topup_sim, new_key
from backend.test_transactions_actions import run, render


def db_call(callback):
    async def execute():
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            return await callback(await anext(dependency))
    return asyncio.run(execute())


def ledger(client, **params):
    response = client.get('/api/transaction-ledger', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def snapshot():
    async def read(db):
        result = {}
        for model in (OperatorTopup, SimLedger, SimCard, SimBalance, Transaction, OperatorBalance, ProfitLedger):
            rows = (await db.execute(select(model.__table__).order_by(*model.__table__.primary_key.columns))).all()
            result[model.__name__] = [tuple(row) for row in rows]
        return result
    return db_call(read)


def test_combined_identity_money_fields_and_no_accounting_changes(client, transactions):
    async def add(db):
        op = await db.scalar(select(Operator).where(Operator.code == 'mtn'))
        row = OperatorTopup(operator_id=op.id, amount_cents=123456789, reference='historical-topup')
        db.add(row)
        await db.commit()
        return row.id
    operator_id = db_call(add)
    before = snapshot()
    page = ledger(client)
    assert page['total'] == len(page['items']) == 8  # five sales, two SIM top-ups, legacy top-up
    assert {r['record_type'] for r in page['items']} == {'sale', 'topup'}
    operator = next(r for r in page['items'] if r['source'] == 'operator_topup')
    assert (operator['id'], operator['reference'], operator['amount_cents']) == (operator_id, 'historical-topup', 123456789)
    assert operator['performed_by_user_id'] is None
    assert operator['performed_by_username'] is None
    for row in page['items']:
        assert row['status'] == 'successful'
        assert type(row['amount_cents']) is int
        assert row['created_at'] and row['reference'] and row['operator_code']
        assert 'idempotency_key' not in row and 'balance_after_cents' not in row
        if row['source'] == 'sim_topup':
            stored = next(r for r in before['SimLedger'] if r[0] == row['id'])
            assert stored[2] == 'topup'
            assert row['sim_card_id'] is not None
            assert row['performed_by_username']
        elif row['record_type'] == 'sale':
            assert 'customer_name' in row and row['customer_phone']
            assert row['funding'] == 'sim' and row['service_code']
    assert ledger(client, status='failed')['items'] == []
    assert ledger(client, status='failed')['total'] == 0
    assert snapshot() == before


@pytest.mark.parametrize('field,value', [('service_code', 'prepaid'), ('performed_by', '1'), ('customer_phone', '0933')])
def test_sale_filters_require_explicit_sale_type(client, transactions, field, value):
    for kind in ('all', 'topup'):
        assert client.get('/api/transaction-ledger', params={'record_type': kind, field: value}).status_code == 422
    page = ledger(client, record_type='sale', **{field: value})
    assert all(row['record_type'] == 'sale' for row in page['items'])
    if field == 'service_code':
        assert page['total'] > 0
        assert all(row['service_code'] == value for row in page['items'])
    if field == 'customer_phone':
        assert page['total'] > 0
        assert all(value in row['customer_phone'] for row in page['items'])


def test_operator_type_status_and_validation(client, transactions):
    for op in ('mtn', 'syriatel'):
        page = ledger(client, operator_code=op)
        assert {r['record_type'] for r in page['items']} == {'sale', 'topup'}
        assert all(r['operator_code'] == op for r in page['items'])
    sales = ledger(client, record_type='sale')
    topups = ledger(client, record_type='topup')
    assert sales['total'] == 5 and topups['total'] == 2
    assert ledger(client, operator_code='missing')['total'] == 0
    for params in ({'limit': 0}, {'limit': 201}, {'offset': -1}, {'record_type': 'refund'}, {'status': 'pending'}):
        assert client.get('/api/transaction-ledger', params=params).status_code == 422


def test_order_ties_and_pagination_have_no_loss_or_duplicates(client, transactions):
    async def tie(db):
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Ledger is append-only: tie new entries at insert time, not by updating history.
        op = await db.scalar(select(Operator).where(Operator.code == 'mtn'))
        sim = await db.get(SimCard, client.sim_ids['mtn'])
        original = await db.scalar(select(SimLedger).where(SimLedger.kind == 'topup'))
        db.add(SimLedger(sim_card_id=sim.id, kind='topup', amount_cents=1,
                         balance_after_cents=0, reference='tie-sim',
                         idempotency_key=new_key(), performed_by_user_id=original.performed_by_user_id,
                         created_at=when))
        sale = await db.scalar(select(Transaction).where(Transaction.status == 'successful'))
        sale.created_at = when
        for i in range(5):
            db.add(OperatorTopup(operator_id=op.id, amount_cents=100 + i,
                                 reference=f'tie-{i}', created_at=when))
        await db.commit()
    db_call(tie)
    all_rows = ledger(client)['items']
    keys = [(r['created_at'], r['source'], r['id']) for r in all_rows]
    assert keys == sorted(keys, reverse=True)
    assert len([r for r in all_rows if r['created_at'].startswith('2026-01-01')]) == 7
    paged = []
    for offset in range(0, len(all_rows) + 2, 2):
        page = ledger(client, limit=2, offset=offset)
        assert page['total'] == len(all_rows)
        assert page['offset'] == offset and page['limit'] == 2
        paged.extend(page['items'])
    assert paged == all_rows
    assert len({(r['source'], r['id']) for r in paged}) == len(paged)


def test_replayed_topup_is_one_persisted_row_and_listing_is_read_only(client, transactions):
    sim_id = client.sim_ids['mtn']
    key = new_key()
    response = topup_sim(client, sim_id, 12345, key)
    assert response.status_code == 200
    before = snapshot()
    for _ in range(2):
        ledger(client)
        assert topup_sim(client, sim_id, 12345, key).status_code == 200
    after = snapshot()
    assert after == before
    assert len([r for r in ledger(client, record_type='topup')['items'] if r['amount_cents'] == 12345]) == 1


@pytest.mark.parametrize('lang,labels', [('en', ['Sales only', 'Top-ups only', 'Previous', 'Next']),
                                        ('ar', ['المبيعات فقط', 'التعبئات فقط', 'السابق', 'التالي'])])
def test_bilingual_type_filters_pagination_accessibility_and_pages(lang, labels):
    html = render(lang)
    assert f'dir="{"rtl" if lang == "ar" else "ltr"}"' in html
    for label in labels:
        assert label in html
    assert 'id="sale-filter-help"' in html and 'aria-label=' in html
    from backend.test_transactions_actions import ROWS
    rows = [dict(ROWS[0], id=i, record_type='sale') for i in range(55)]
    result = run([{'click': 'tx-next'}], rows=rows, lang=lang)
    assert len(result['rows']) == 5
    assert 'offset=50' in result['requests'][-1]['url']
    result = run([{'setFilter': {'id': 'filter-type', 'value': 'sale'}},
                  {'setFilter': {'id': 'filter-type', 'value': 'all'}}], rows=rows, lang=lang)
    assert result['requests'][-1]['url'] == '/api/transaction-ledger'


def test_seller_privacy_authentication_and_csrf(client, transactions):
    from backend import auth
    from backend.database import User
    from backend.auth_test_support import _password_hash

    async def make_seller(db):
        user = User(username='ledger-seller', role='seller', password_hash=_password_hash(),
                    must_change_password=False)
        db.add(user)
        await db.flush()
        # Both shared and private histories exist. The seller owns neither private SIM.
        sim = await db.scalar(select(SimCard).where(SimCard.id == client.sim_ids['mtn']))
        owner_id = await db.scalar(select(User.id).where(User.role == "admin"))
        sim.is_shared = False
        sim.owner_user_id = owner_id
        token = await auth._start_session(db, user)
        await db.commit()
        return token
    token = db_call(make_seller)
    client.cookies.clear()
    assert client.get('/api/transaction-ledger').status_code == 401
    client.cookies.set(auth.SESSION_COOKIE, token)
    page = ledger(client)
    for row in page['items']:
        if row['operator_code'] == 'mtn':
            assert row['sim_card_id'] is None and row['sim_display_name'] is None
        else:
            assert row['sim_card_id'] is not None
    client.headers.pop(auth.CSRF_HEADER, None)
    before = snapshot()
    assert client.post('/api/sims/1/topup', json={'amount_cents': 10, 'topup_key': new_key()}).status_code == 403
    assert snapshot() == before
