"""XLSX package/reader, filter parity, completeness, text safety and access."""
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from itertools import product
import re
from types import SimpleNamespace
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from openpyxl import load_workbook
import pytest
from sqlalchemy import select

from backend import auth, xlsx
from backend.database import (Operator, OperatorTopup, Transaction, User, SimCard,
                              OperatorCreditRefund, SimBalance, SimLedger, OperatorBalance,
                              ProfitLedger, CashRegister)
from backend.auth_test_support import _password_hash
from backend.test_transaction_ledger import db_call, ledger
from backend.test_transactions_list import client, transactions
from backend.test_transactions_actions import render, run, ROWS

URL = '/api/transaction-ledger/export'


def download(client, **params):
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    return response


def workbook(response):
    return load_workbook(BytesIO(response.content), data_only=False)


def expected(item, lang='en'):
    sale = item['record_type'] == 'sale'
    labels = xlsx.LABELS[lang]
    return [labels[item['record_type']], labels[item['source']], item['reference'],
            datetime.fromisoformat(item['created_at']).strftime('%Y-%m-%d %H:%M:%S'),
            item['operator_name'], Decimal(item['amount_cents']) / 100, labels[item['status']],
            item['customer_name'] if sale else None, item['customer_phone'] if sale else None,
            item['service_name'] if sale else None, item['performed_by_username'], item['sim_display_name']]


def values(sheet):
    return [list(row) for row in sheet.iter_rows(min_row=2, values_only=True)]


def financial_snapshot():
    async def read(db):
        return {model.__name__: [tuple(row) for row in (await db.execute(
            select(model.__table__).order_by(*model.__table__.primary_key.columns))).all()]
                for model in (Transaction, OperatorTopup, SimLedger, SimBalance, OperatorBalance,
                              ProfitLedger, CashRegister, OperatorCreditRefund)}
    return db_call(read)


@pytest.mark.parametrize('lang', ['en', 'ar'])
def test_package_headers_localization_formatting_and_read_only(client, transactions, lang):
    before = financial_snapshot()
    response = download(client, lang=lang)
    assert response.headers['content-type'] == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    assert re.fullmatch(r'attachment; filename="uniter-transactions-\d{8}\.xlsx"', response.headers['content-disposition'])
    assert response.headers['cache-control'] == 'no-store'
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            ET.fromstring(archive.read(name))
        sheet_xml = archive.read('xl/worksheets/sheet1.xml')
        assert b'<ns0:f' not in sheet_xml
    book = workbook(response)
    assert book.sheetnames == ['Transactions']
    sheet = book.active
    assert [c.value for c in sheet[1]] == xlsx.HEADERS[lang]
    assert sheet.freeze_panes == 'A2'
    assert sheet.auto_filter.ref == f'A1:L{sheet.max_row}'
    assert bool(sheet.sheet_view.rightToLeft) == (lang == 'ar')
    assert sheet.column_dimensions['C'].width >= 30
    assert sheet['A1'].font.bold
    assert sheet['F2'].data_type == 'n' and sheet['F2'].number_format == '#,##0.00'
    assert re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', sheet['D2'].value)
    rows = ledger(client)['items']
    for actual, item in zip(values(sheet), rows, strict=True):
        exp = expected(item, lang)
        assert actual[:5] == exp[:5] and actual[6:] == exp[6:]
        assert Decimal(str(actual[5])) == exp[5]
    assert financial_snapshot() == before


def test_complete_export_ignores_ui_limit_offset_and_batches_hydration(client, transactions):
    async def insert(db):
        op = await db.scalar(select(Operator).where(Operator.code == 'mtn'))
        for i in range(255):
            db.add(OperatorTopup(operator_id=op.id, amount_cents=12345,
                                 reference=f'export-{i:03}', created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        await db.commit()
    db_call(insert)
    before = financial_snapshot()
    page = ledger(client)
    assert len(page['items']) == 50 and page['total'] == 262
    sheet = workbook(download(client, limit=1, offset=200)).active
    assert sheet.max_row == 263
    api_rows = []
    for offset in (0, 200):
        api_rows.extend(ledger(client, limit=200, offset=offset)['items'])
    exported = values(sheet)
    assert [r[2] for r in exported] == [r['reference'] for r in api_rows]
    assert len(set(r[2] for r in exported)) == 262
    legacy = [r for r in exported if r[1] == 'Operator credit top-up']
    assert len(legacy) == 255
    assert all(r[0] == 'Top-up' and r[5] == 123.45 for r in legacy)
    assert all(r[7:] == [None] * 5 for r in legacy)  # no invented sale/performer/SIM
    with ZipFile(BytesIO(download(client).content)) as archive:
        assert b'>123.45<' in archive.read('xl/worksheets/sheet1.xml')
    assert financial_snapshot() == before


def test_filter_combinations_and_failed_sales_match_api(client, transactions):
    # Exercise the full supported filter cross-product, not just individual keys.
    for kind, operator, status in product(('all', 'sale', 'topup'), (None, 'mtn', 'syriatel', 'missing'), ('successful', 'failed')):
        params = {'record_type': kind, 'status': status}
        if operator:
            params['operator_code'] = operator
        assert [r[2] for r in values(workbook(download(client, **params)).active)] == [r['reference'] for r in ledger(client, **params)['items']]
    for service, performer, phone in product((None, 'prepaid', 'postpaid'), (None, 1, 99999), (None, '0933', 'missing')):
        params = {'record_type': 'sale', 'operator_code': 'mtn'}
        params.update({k: v for k, v in [('service_code', service), ('performed_by', performer), ('customer_phone', phone)] if v is not None})
        api = ledger(client, **params)['items']
        exported = values(workbook(download(client, **params)).active)
        assert [r[2] for r in exported] == [r['reference'] for r in api]
        assert all(r[0] == 'Sale' and r[6] == 'Successful' for r in exported)


@pytest.mark.parametrize('params', [
    {'record_type': 'all', 'customer_phone': '09'}, {'record_type': 'topup', 'performed_by': 1},
    {'service_code': 'prepaid'}, {'record_type': 'no'}, {'status': 'pending'},
    {'operator_code': ''}, {'customer_phone': 'x' * 33, 'record_type': 'sale'},
    {'performed_by': 0, 'record_type': 'sale'}, {'service_code': '', 'record_type': 'sale'},
])
def test_invalid_filter_parity(client, params):
    assert client.get(URL, params=params).status_code == 422
    assert client.get('/api/transaction-ledger', params=params).status_code == 422


@pytest.mark.parametrize('prefix', ['=', '+', '-', '@'])
def test_formula_payloads_are_literal_strings_with_whitespace(prefix):
    payload = ' \t\n' + prefix + 'SUM(1,2)<&"عربي'
    item = SimpleNamespace(record_type='sale', source='transaction', reference=payload,
                           created_at=datetime(2026, 1, 1), operator_name=payload, amount_cents=12345,
                           status='successful', customer_name=payload, customer_phone=payload,
                           service_name=payload, performed_by_username=payload, sim_display_name=payload)
    content = xlsx.ledger_workbook([item], 'ar')
    sheet = load_workbook(BytesIO(content), data_only=False).active
    for column in ('C', 'E', 'H', 'I', 'J', 'K', 'L'):
        cell = sheet[f'{column}2']
        assert cell.data_type == 's' and cell.value == payload
    with ZipFile(BytesIO(content)) as archive:
        root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        assert not root.findall('.//{' + xlsx.NS + '}f')
        assert all(c.attrib.get('t') == 'inlineStr' for c in root.findall('.//{' + xlsx.NS + '}c') if c.attrib['r'][0] != 'F')


def test_real_sale_arabic_snapshot_and_formula_text_unchanged(client, transactions):
    async def edit(db):
        row = await db.scalar(select(Transaction).where(Transaction.status == 'successful'))
        row.customer_name = '  =عربي<&>'
        row.customer_phone = ' +12345'
        await db.commit()
        return row.reference
    ref = db_call(edit)
    before = financial_snapshot()
    exported = next(r for r in values(workbook(download(client, lang='ar')).active) if r[2] == ref)
    assert exported[7:9] == ['  =عربي<&>', ' +12345']
    assert financial_snapshot() == before


def test_private_sim_metadata_auth_and_readonly_download(client, transactions):
    async def seller(db):
        user = User(username='export-seller', role='seller', password_hash=_password_hash(), must_change_password=False)
        db.add(user)
        await db.flush()
        owner = await db.scalar(select(User.id).where(User.role == 'admin'))
        sim = await db.get(SimCard, client.sim_ids['mtn'])
        sim.is_shared, sim.owner_user_id = False, owner
        token = await auth._start_session(db, user)
        await db.commit()
        return token
    token = db_call(seller)
    client.cookies.clear()
    assert client.get(URL).status_code == 401
    client.cookies.set(auth.SESSION_COOKIE, token)
    client.headers.pop(auth.CSRF_HEADER, None)  # GET is read-only: no CSRF exemption added
    before = financial_snapshot()
    api = ledger(client)['items']
    rows = values(workbook(download(client)).active)
    assert [r[2] for r in rows] == [r['reference'] for r in api]
    for row, item in zip(rows, api, strict=True):
        assert row[11] == item['sim_display_name']
        if item['operator_code'] == 'mtn':
            assert row[11] is None and item['sim_card_id'] is None
    assert financial_snapshot() == before


@pytest.mark.parametrize('lang,label', [('en', 'Export XLSX'), ('ar', 'تصدير XLSX')])
def test_ui_export_uses_active_filters_not_page_offset(lang, label):
    html = render(lang)
    assert label in html and 'id="tx-export"' in html and 'aria-label=' in html
    rows = [dict(ROWS[0], id=i) for i in range(55)]
    result = run([{'setFilter': {'id': 'filter-type', 'value': 'sale'}},
                  {'setFilter': {'id': 'filter-operator', 'value': 'mtn'}},
                  {'setFilter': {'id': 'filter-service', 'value': 'prepaid'}},
                  {'setFilter': {'id': 'filter-performer', 'value': '1'}},
                  {'setFilter': {'id': 'filter-phone', 'value': '09'}},
                  {'click': 'tx-next'}, {'click': 'tx-export'}], rows=rows, lang=lang)
    href = result['elements']['tx-export']['href']
    assert href == f'{URL}?record_type=sale&operator_code=mtn&service_code=prepaid&customer_phone=09&performed_by=1&lang={lang}'
    assert 'offset' not in href and 'limit' not in href
    assert 'offset=50' in result['requests'][-1]['url']
    assert not any(r['url'].startswith(URL) for r in result['requests'])  # native authenticated download


def test_excel_limits_invalid_xml_and_safe_filename_inputs(client, transactions, monkeypatch):
    assert client.get(URL, params={'lang': 'en\r\nX-Injected: yes'}).status_code == 422
    monkeypatch.setattr(xlsx, 'MAX_DATA_ROWS', 1)
    assert client.get(URL).status_code == 413
    with pytest.raises(ValueError, match='row limit'):
        xlsx.ledger_workbook([None, None])
    item = SimpleNamespace(record_type='topup', source='operator_topup', reference='a\x00b\x0bc<&>',
                           created_at=datetime(2026, 1, 1), operator_name='MTN', amount_cents=1,
                           status='successful', performed_by_username=None, sim_display_name=None)
    book = load_workbook(BytesIO(xlsx.ledger_workbook([item])))
    assert book.active['C2'].value == 'abc<&>'
    assert book.active['F2'].value == 0.01
    assert book.active['K2'].value is None
