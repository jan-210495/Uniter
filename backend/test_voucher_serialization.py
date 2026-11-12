"""Slice 2.7: exact catalog/display values versus operator USSD serialization."""
import asyncio
from contextlib import aclosing
from decimal import Decimal, ROUND_HALF_EVEN

import pytest
from sqlalchemy import select

from backend.database import ProfitLedger, Transaction, get_db
from backend.main import app
from backend.seed import generate_ussd
from backend.test_pos import client  # disposable tmp_path database
from backend.test_pos_vouchers import VOUCHERS, posted_amount_cents, run_pos, voucher_groups


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_http_catalog_exact_order_labels_and_uniqueness(client, lang):
    html = client.get('/pos', params={'lang': lang}).text
    groups = voucher_groups(html)
    for operator, values in VOUCHERS.items():
        assert groups[operator][1] == values
        assert len(values) == len(set(values))
        for value in values:
            assert f'data-amount="{value}"' in html
    assert len(groups['mtn'][1]) == 46
    assert len(groups['syriatel'][1]) == 48


@pytest.mark.parametrize('service,mode', [('prepaid', '1'), ('postpaid', '5')])
def test_every_voucher_generator_preserves_operator_contract(client, service, mode):
    async def check():
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            db = await anext(dependency)
            for operator, values in VOUCHERS.items():
                for value in values:
                    cents = int(Decimal(value) * 100)
                    code = await generate_ussd(db, operator, service, '0933123456', cents)
                    if operator == 'syriatel':
                        expected = f'*150*1*67858*{mode}*{value.replace(".", "")}*0933123456*0933123456#'
                    else:
                        expected = f'*{"150" if service == "prepaid" else "154"}*65477*0933123456*{value}#'
                    assert code == expected
                    assert '.' not in code
    asyncio.run(check())


@pytest.mark.parametrize('lang', ['en', 'ar'])
@pytest.mark.parametrize('value,cents,summary,total', [
    ('1.92', 192, '1.92', '2.02'),
    ('4.80', 480, '4.8', '5.04'),
    ('62.50', 6250, '62.5', '65.62'),
    ('192.30', 19230, '192.3', '201.92'),
    ('432.69', 43269, '432.69', '454.32'),
    ('3846.15', 384615, '3,846.15', '4,038.46'),
    ('12.34', 1234, '12.34', '12.96'),  # custom amount, not a voucher
    ('12.3', 1230, '12.3', '12.92'),
])
def test_display_and_payload_remain_decimal(lang, value, cents, summary, total):
    select_amount = ({'voucher': {'operator': 'syriatel', 'amount': value}}
                     if value in VOUCHERS['syriatel'] else
                     {'values': {'amount': value}, 'id': 'amount', 'event': 'input'})
    result = run_pos([
        {'values': {'operator': 'syriatel'}, 'id': 'operator', 'event': 'change'},
        select_amount,
        {'values': {'phone': '0933123456'}, 'id': 'charge-form', 'event': 'submit'},
    ], lang=lang)
    assert result['elements']['amount']['value'] == value
    assert result['elements']['summary-amount']['text'] == summary
    assert result['elements']['summary-net']['text'] == total
    assert posted_amount_cents(result) == cents


@pytest.mark.parametrize('service,mode', [('prepaid', '1'), ('postpaid', '5')])
@pytest.mark.parametrize('cents', [192, 480, 6250, 43269, 384615, 1234, 1230, 1200, 0, 1])
def test_preview_commit_persistence_and_accounting(client, service, mode, cents):
    funding = 1_000_000
    assert client.post('/api/operators/topup', json={
        'operator_code': 'syriatel', 'amount_cents': funding,
    }).status_code == 200
    balances_before = client.get('/api/operators/balance').json()
    payload = {'operator_code': 'syriatel', 'service_code': service,
               'customer_phone': '0933123456', 'amount_cents': cents}
    preview = client.post('/api/transactions/preview', json=payload)
    assert preview.status_code == 200
    code = f'*150*1*67858*{mode}*{cents}*0933123456*0933123456#'
    assert preview.json()['ussd_string'] == code
    assert client.get('/api/operators/balance').json() == balances_before
    assert client.get('/api/transactions').json() == []
    assert client.get('/api/customers').json() == []

    async def ledger_rows():
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            db = await anext(dependency)
            return (await db.scalars(select(ProfitLedger))).all()
    assert asyncio.run(ledger_rows()) == []
    commission = int((Decimal(cents) * Decimal('0.05')).quantize(Decimal('1'), rounding=ROUND_HALF_EVEN))
    pricing = client.get('/api/commission/calculate', params=payload).json()
    assert pricing['commission_cents'] == commission
    assert pricing['total_cents'] == cents + commission
    commit_payload = dict(payload, commit_key=preview.json()['commit_key'])
    commit = client.post('/api/transactions', json=commit_payload)
    assert commit.status_code == 200
    sale = commit.json()
    assert sale['ussd_string'] == code
    assert sale['amount_cents'] == cents
    assert sale['status'] == 'successful'
    assert client.post('/api/transactions', json=commit_payload).json() == sale

    async def check_stored():
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            db = await anext(dependency)
            row = (await db.scalars(select(Transaction))).one()
            assert (row.amount_cents, row.ussd_string) == (cents, code)
            entry = (await db.scalars(select(ProfitLedger))).one()
            assert (entry.amount_cents, entry.commission_cents, entry.profit_cents) == (cents, commission, commission)
    asyncio.run(check_stored())
    balance = next(b for b in client.get('/api/operators/balance').json() if b['operator_code'] == 'syriatel')
    assert balance['balance_cents'] == funding - cents
    failed = client.patch(f'/api/transactions/{sale["id"]}/status', json={'status': 'failed'})
    assert failed.status_code == 200
    balance = next(b for b in client.get('/api/operators/balance').json() if b['operator_code'] == 'syriatel')
    assert balance['balance_cents'] == funding


@pytest.mark.parametrize('value', ['', '-1', '1.234', 'abc', '1e3', '90071992547409.92'])
def test_invalid_custom_syriatel_amount_never_posts(value):
    result = run_pos([{'values': {'operator': 'syriatel', 'phone': '0933123456', 'amount': value},
                       'id': 'charge-form', 'event': 'submit'}])
    assert not [r for r in result['requests'] if r['method'] == 'POST']


@pytest.mark.parametrize('cents', [-1, 1.234, 9223372036854775808])
def test_invalid_api_cents_rejected_for_preview_and_commit(client, cents):
    payload = {'operator_code': 'syriatel', 'service_code': 'prepaid',
               'customer_phone': '0933123456', 'amount_cents': cents,
               'commit_key': 'invalidamount00000000000000000001'}
    for route in ['/api/transactions/preview', '/api/transactions']:
        assert client.post(route, json=payload).status_code == 422
    assert client.get('/api/transactions').json() == []
