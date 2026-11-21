"""SYP input boundaries, running actual rendered JavaScript and isolated APIs."""
import itertools
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.sim_test_support import create_sim, fund_company, remember_sim, sim_balance, with_sim
from backend.test_pos import client  # isolated, freshly seeded tmp_path database


def run_form(client, page, steps, sim_id=None):
    if not shutil.which('node'):
        pytest.skip('Node.js required to execute rendered form handlers')
    html = client.get(page).text
    scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
    # Slice 4.1: hand the script the SIM card choices the server rendered.
    sims = re.search(r'<script type="application/json" id="pos-sim-data">(.*?)</script>', html, re.S)
    result = subprocess.run(['node', str(Path(__file__).with_name('money_form_harness.cjs'))],
                            input=json.dumps({'script': scripts[0], 'steps': steps, 'simId': sim_id,
                                              'posSims': sims.group(1) if sims else ''}),
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def pos_ready(client):
    """A funded MTN SIM card saved as this account's default, as the POS expects."""
    sim_id = fund_company(client, 'mtn', 5_000_000)
    assert client.put('/api/pos/default-sim', json={'operator_code': 'mtn', 'sim_id': sim_id}).status_code == 200
    return sim_id


def test_ussd_absence_can_be_balance_rejection_not_generator_failure(client):
    payload={'customer_phone':'0944123456','operator_code':'mtn','service_code':'prepaid','amount_cents':50_000,
             'commit_key':new_commit_key()}
    rejected=client.post('/api/transactions',json=with_sim(client, payload))
    assert rejected.status_code == 400
    assert rejected.json()['detail'] == 'Insufficient SIM balance'
    assert client.get('/api/transactions').json() == []
    fund_company(client, 'mtn', 5_000_000)
    payload['commit_key']=new_commit_key()
    generated=client.post('/api/transactions',json=with_sim(client, payload))
    assert generated.status_code == 200
    assert generated.json()['ussd_string'] == '*150*65477*0944123456*500#'


def topup_form(client, value, sim_id=None):
    return run_form(client, '/topup', [{'values': {'topup:input[name="amount"]': value},
                                      'id': 'topup', 'event': 'submit'}], sim_id=sim_id)


def test_topup_50000_syp_survives_refresh_reload_and_funds_500_charge(client):
    # Slice 4.1: money is loaded onto a SIM card from its Operators-page form.
    sim_id = create_sim(client, 'mtn')['id']
    remember_sim(client, 'mtn', sim_id)
    result = topup_form(client, '50000', sim_id)
    request = next(r for r in result['requests'] if r['method'] == 'POST')
    assert request['url'] == f'/api/sims/{sim_id}/topup'
    payload = request['body']
    assert payload['amount_cents'] == 5_000_000
    assert result['elements']['card:[data-field="sim-balance"]']['text'] == '50,000'
    assert client.post(request['url'], json=payload).status_code == 200
    assert '>50,000</span>' in client.get('/topup').text
    charge = client.post('/api/transactions', json=with_sim(client, {'customer_phone': '0944123456',
        'operator_code': 'mtn', 'service_code': 'prepaid', 'amount_cents': 50_000,
        'commit_key': new_commit_key()}))
    assert charge.status_code == 200
    assert charge.json()['ussd_string'].endswith('*500#')
    assert sim_balance(client, sim_id) == 4_950_000
    assert '>49,500</span>' in client.get('/topup').text
    # The legacy company credit never moved.
    assert client.get('/api/operators/balance').json()[0]['balance_cents'] == 0


def test_exact_face_balance_never_goes_negative(client):
    fund_company(client, 'mtn', 50_000)
    payload = {'customer_phone':'0944123456','operator_code':'mtn','service_code':'prepaid','amount_cents':50_000,
               'commit_key':new_commit_key()}
    assert client.post('/api/transactions', json=with_sim(client, payload)).status_code == 200
    assert sim_balance(client, client.sim_ids['mtn']) == 0
    payload['amount_cents'] = 1
    payload['commit_key'] = new_commit_key()
    assert client.post('/api/transactions', json=with_sim(client, payload)).status_code == 400
    assert sim_balance(client, client.sim_ids['mtn']) == 0


def test_pos_500_summary_matches_customer_total_api(client):
    pos_ready(client)
    result = run_form(client, '/pos', [{'values':{'amount':'500','phone':'0944123456','operator':'mtn','service':'prepaid'},'id':'amount','event':'input'},
                                     {'id':'charge-form','event':'submit'}])
    assert result['elements']['summary-net']['text'] == '600'
    payload = next(r['body'] for r in result['requests'] if r['url']=='/api/transactions/preview')
    assert payload['amount_cents'] == 50_000
    calculation=client.get('/api/commission/calculate', params={'amount_cents':50_000,'operator_code':'mtn','service_code':'prepaid'}).json()
    assert calculation['total_cents'] == 60_000
    assert calculation['net_cents'] == 40_000  # retained legacy API field


@pytest.mark.parametrize('value', ['', '-1', '1.5', 'abc', '1e30', '9007199254740991'])

def test_invalid_whole_syp_never_posts(client,value):
    result=topup_form(client,value)
    assert not [r for r in result['requests'] if r['method'] in ('POST','PUT')]


@pytest.mark.parametrize('phone', ['0944123456', '+963 944-123-456', '00963 944-123-456'])
def test_pos_real_handler_accepts_normalized_phone(client,phone):
    sim_id=pos_ready(client)
    result=run_form(client,'/pos',[{'values':{'phone':phone,'amount':'500','operator':'mtn','service':'prepaid'},'id':'charge-form','event':'submit'}])
    posts=[r for r in result['requests'] if r['url']=='/api/transactions/preview']
    assert len(posts)==1
    assert posts[0]['body']['customer_phone']=='0944123456'
    assert posts[0]['body']['sim_card_id']==sim_id  # Slice 4.1: the saved default SIM card


@pytest.mark.parametrize('value', ['', '-1', '1.234', 'abc', '1e3', '90071992547409.92'])
def test_pos_invalid_or_unsafe_amount_does_not_post(client,value):
    pos_ready(client)  # a usable SIM card: only the amount can stop the post
    result=run_form(client,'/pos',[{'values':{'phone':'0944123456','amount':value},'id':'charge-form','event':'submit'}])
    assert not [r for r in result['requests'] if r['method']=='POST']


_COMMIT_KEYS = itertools.count(1)


def new_commit_key() -> str:
    """A fresh commit key: every call is a deliberate, distinct sale."""
    return f"boundarykey{next(_COMMIT_KEYS):021d}"


@pytest.mark.parametrize('lang', ['en', 'ar'])
@pytest.mark.parametrize('service,other', [('prepaid', 'postpaid'), ('postpaid', 'prepaid')])
def test_settings_independent_controls_send_only_selected_key(client, lang, service, other):
    html = client.get('/settings', params={'lang': lang}).text
    assert f'id="commission-input-{service}"' in html
    assert f'id="commission-current-{other}"' in html
    result = run_form(client, '/settings?lang=' + lang, [{
        'values': {'commission-input-' + service: '0.12345678901234567890123456789'},
        'id': 'commission-form-' + service, 'event': 'submit'
    }])
    requests = [r for r in result['requests'] if r['method'] == 'PUT']
    assert len(requests) == 1
    assert requests[0]['body'] == {'commission_rate_' + service: '0.12345678901234567890123456789'}
    elements = result['elements']
    assert '0.12345678901234567890123456789' in elements['commission-current-' + service]['text']
    assert ('0.20' if other == 'prepaid' else '0.06') in elements['commission-current-' + other]['text']
    assert elements['commission-feedback-' + service]['text']
    assert elements['commission-feedback-' + other]['text'] == ''


@pytest.mark.parametrize('lang', ['en', 'ar'])
@pytest.mark.parametrize('service,amount', [('prepaid', 25), ('prepaid', 15), ('postpaid', 25), ('postpaid', 75), ('postpaid', 5000)])
def test_pos_service_switch_matches_exact_cents_api_and_commit(client, lang, service, amount):
    from contextlib import aclosing
    import asyncio
    from sqlalchemy import select
    from backend.database import ProfitLedger, get_db
    from backend.main import app
    pos_ready(client)
    other = 'postpaid' if service == 'prepaid' else 'prepaid'
    result = run_form(client, '/pos?lang=' + lang, [
        {'values': {'amount': format(amount / 100, '.2f'), 'service': other}, 'id': 'amount', 'event': 'input'},
        {'values': {'service': service}, 'id': 'service', 'event': 'change'}
    ])
    calculation = client.get('/api/commission/calculate', params={
        'operator_code': 'mtn', 'service_code': service, 'amount_cents': amount
    }).json()
    def display(cents):
        return f'{cents // 100:,}' + (('.' + f'{cents % 100:02d}'.rstrip('0')) if cents % 100 else '')
    assert result['elements']['summary-rate']['text'] == ('20%' if service == 'prepaid' else '6%')
    assert result['elements']['summary-commission']['text'] == display(calculation['commission_cents'])
    assert result['elements']['summary-net']['text'] == display(calculation['total_cents'])
    response = client.post('/api/transactions', json=with_sim(client, {
        'customer_phone': '0944123456', 'operator_code': 'mtn', 'service_code': service,
        'amount_cents': amount, 'commit_key': new_commit_key()
    }))
    assert response.status_code == 200
    async def check():
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            entry = await session.scalar(select(ProfitLedger).where(ProfitLedger.transaction_id == response.json()['id']))
            assert entry.commission_cents == entry.profit_cents == calculation['commission_cents']
    asyncio.run(check())
