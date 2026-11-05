"""SYP input boundaries, running actual rendered JavaScript and isolated APIs."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.test_pos import client  # isolated, freshly seeded tmp_path database


def run_form(client, page, steps):
    if not shutil.which('node'):
        pytest.skip('Node.js required to execute rendered form handlers')
    html = client.get(page).text
    scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
    result = subprocess.run(['node', str(Path(__file__).with_name('money_form_harness.cjs'))],
                            input=json.dumps({'script': scripts[0], 'steps': steps}),
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_ussd_absence_can_be_balance_rejection_not_generator_failure(client):
    payload={'customer_phone':'0944123456','operator_code':'mtn','service_code':'prepaid','amount_cents':50_000}
    rejected=client.post('/api/transactions',json=payload)
    assert rejected.status_code == 400
    assert rejected.json()['detail'] == 'Insufficient operator balance'
    assert client.get('/api/transactions').json() == []
    client.post('/api/operators/topup',json={'operator_code':'mtn','amount_cents':5_000_000})
    generated=client.post('/api/transactions',json=payload)
    assert generated.status_code == 200
    assert generated.json()['ussd_string'] == '*150*65477*0944123456*500#'


def topup_form(client, value):
    return run_form(client, '/topup', [{'values': {'topup:input[name="amount"]': value},
                                      'id': 'topup', 'event': 'submit'}])


def test_topup_50000_syp_survives_refresh_reload_and_funds_500_charge(client):
    result = topup_form(client, '50000')
    payload = next(r['body'] for r in result['requests'] if r['method'] == 'POST')
    assert payload['amount_cents'] == 5_000_000
    assert result['elements']['card:[data-field="balance"]']['text'] == '50,000'
    assert client.post('/api/operators/topup', json=payload).status_code == 200
    assert '>50,000</span>' in client.get('/topup').text
    charge = client.post('/api/transactions', json={'customer_phone': '0944123456',
        'operator_code': 'mtn', 'service_code': 'prepaid', 'amount_cents': 50_000})
    assert charge.status_code == 200
    assert charge.json()['ussd_string'].endswith('*500#')
    assert client.get('/api/operators/balance').json()[0]['balance_cents'] == 4_950_000
    assert '>49,500</span>' in client.get('/topup').text


def test_exact_face_balance_never_goes_negative(client):
    client.post('/api/operators/topup', json={'operator_code': 'mtn', 'amount_cents': 50_000})
    payload = {'customer_phone':'0944123456','operator_code':'mtn','service_code':'prepaid','amount_cents':50_000}
    assert client.post('/api/transactions', json=payload).status_code == 200
    assert client.get('/api/operators/balance').json()[0]['balance_cents'] == 0
    payload['amount_cents'] = 1
    assert client.post('/api/transactions', json=payload).status_code == 400


def test_pos_500_summary_matches_customer_total_api(client):
    result = run_form(client, '/pos', [{'values':{'amount':'500','phone':'0944123456','operator':'mtn','service':'prepaid'},'id':'amount','event':'input'},
                                     {'id':'charge-form','event':'submit'}])
    assert result['elements']['summary-net']['text'] == '525'
    payload = next(r['body'] for r in result['requests'] if r['url']=='/api/transactions')
    assert payload['amount_cents'] == 50_000
    calculation=client.get('/api/commission/calculate', params={'amount_cents':50_000,'operator_code':'mtn','service_code':'prepaid'}).json()
    assert calculation['total_cents'] == 52_500
    assert calculation['net_cents'] == 47_500  # retained legacy API field


def debt_steps(value, edit=False):
    steps = [{'id':'customers-body','event':'click','action':'debt',
              'row':{'customerId':'1','name':'Ahmad','phone':'0944123456'}}]
    if edit:
        steps.append({'id':'debt-entries','event':'click','action':'edit',
                      'row':{'debtId':'1','amount':'50000','description':'Charge','type':'charge'}})
    if value is not None:
        steps.append({'values':{'debt-amount':value,'debt-type':'charge','debt-description':'Charge'}})
    steps.append({'id':'debt-form','event':'submit'})
    return steps


@pytest.mark.parametrize('edit', [False, True])
def test_debt_add_edit_syp_roundtrip(client, edit):
    customer=client.post('/api/customers',json={'name':'Ahmad','phone':'0944123456'}).json()
    result=run_form(client, '/customers', debt_steps('500', edit))
    request=next(r for r in result['requests'] if r['method'] in ('POST','PUT'))
    assert request['body']['amount_cents'] == 50_000
    if edit:
        client.post('/api/customers/1/debt',json=request['body'])
    assert client.request(request['method'], request['url'], json=request['body']).status_code == 200
    assert client.get(f"/api/customers/{customer['id']}/balance").json()['total_debt_cents'] == 50_000
    prefill=run_form(client,'/customers',debt_steps(None, True))
    edit_request=next(r for r in prefill['requests'] if r['method']=='PUT')
    assert edit_request['body']['amount_cents'] == 50_000


def test_settlement_payment_syp(client):
    client.post('/api/customers',json={'name':'Rami','phone':'0944123456'})
    client.post('/api/customers/1/debt',json={'amount_cents':50_000,'type':'charge'})
    result=run_form(client,'/settlement',[{'values':{'customer-phone':'0944123456','payment-amount':'200'},'id':'payment-form','event':'submit'}])
    request=next(r for r in result['requests'] if r['method']=='POST')
    assert request['body']['amount_cents'] == 20_000
    assert client.post(request['url'],json=request['body']).status_code == 200
    assert client.get('/api/customers/1/balance').json()['total_debt_cents'] == 30_000


@pytest.mark.parametrize('value', ['', '-1', '1.5', 'abc', '1e30', '9007199254740991'])
@pytest.mark.parametrize('page', ['topup','customers','settlement'])
def test_invalid_whole_syp_never_posts(client,page,value):
    if page=='topup': result=topup_form(client,value)
    elif page=='customers': result=run_form(client,'/customers',debt_steps(value))
    else: result=run_form(client,'/settlement',[{'values':{'customer-phone':'0944123456','payment-amount':value},'id':'payment-form','event':'submit'}])
    assert not [r for r in result['requests'] if r['method'] in ('POST','PUT')]


@pytest.mark.parametrize('phone', ['0944123456', '+963 944-123-456', '00963 944-123-456'])
def test_pos_real_handler_accepts_normalized_phone(client,phone):
    result=run_form(client,'/pos',[{'values':{'phone':phone,'amount':'500','operator':'mtn','service':'prepaid'},'id':'charge-form','event':'submit'}])
    posts=[r for r in result['requests'] if r['url']=='/api/transactions']
    assert len(posts)==1
    assert posts[0]['body']['customer_phone']=='0944123456'


@pytest.mark.parametrize('value', ['', '-1', '1.234', 'abc', '1e3', '90071992547409.92'])
def test_pos_invalid_or_unsafe_amount_does_not_post(client,value):
    result=run_form(client,'/pos',[{'values':{'phone':'0944123456','amount':value},'id':'charge-form','event':'submit'}])
    assert not [r for r in result['requests'] if r['method']=='POST']
