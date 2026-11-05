"""Settlement markup and real API-backed script refresh regression tests."""
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from backend.test_pos import client  # fresh tmp_path SQLite fixture, never the user DB


class SettlementMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.elements = {}
        self.ledger_body = False
        self.ledger_headers = []
        self.in_header = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.elements[attrs['id']] = attrs.get('class', '')
        in_ledger = attrs.get('id') == 'ledger-container' or any(item[1] for item in self.stack)
        if tag == 'tbody' and in_ledger:
            self.ledger_body = True
        if tag == 'th' and in_ledger:
            self.in_header = True
        if tag not in {'input', 'meta', 'link', 'br', 'hr', 'img'}:
            self.stack.append((tag, in_ledger))

    def handle_endtag(self, tag):
        if tag == 'th':
            self.in_header = False
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.in_header and data.strip():
            self.ledger_headers.append(data.strip())


@pytest.mark.parametrize('lang,headers', [
    ('en', ['Date', 'Type', 'Amount (SYP)', 'Description']),
    ('ar', ['التاريخ', 'النوع', 'المبلغ (ل.س)', 'الوصف']),
])
def test_rendered_settlement_has_bilingual_ledger_body(client, lang, headers):
    html = client.get('/settlement', params={'lang': lang}).text
    markup = SettlementMarkup(html)
    assert markup.ledger_body, 'loadLedger requires a real #ledger-container tbody'
    assert markup.ledger_headers == headers


def test_settlement_phone_hint_includes_accepted_098_prefix(client):
    for lang in ('en', 'ar'):
        html = client.get('/settlement', params={'lang': lang}).text
        assert re.search(r'<p[^>]*>[^<]*093[^<]*098[^<]*099[^<]*</p>', html)


def test_payment_refreshes_summary_and_ledger_using_rendered_dom_and_real_api(client):
    if not shutil.which('node'):
        pytest.skip('Node.js required for rendered script execution')
    customer = client.post('/api/customers', json={'name': 'Rami', 'phone': '0944123456'}).json()
    assert client.post(f"/api/customers/{customer['id']}/debt", json={
        'amount_cents': 50_000, 'type': 'charge', 'description': 'Initial charge',
    }).status_code == 200
    html = client.get('/settlement').text
    markup = SettlementMarkup(html)
    payload = {'elements': markup.elements, 'ledgerBody': markup.ledger_body,
               'script': re.findall(r'<script>(.*?)</script>', html, re.S)[0]}
    requests = []
    with subprocess.Popen(['node', str(Path(__file__).with_name('settlement_dom_harness.cjs'))],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) as process:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        process.stdin.write(json.dumps(payload) + '\n')
        process.stdin.flush()
        while True:
            line = process.stdout.readline()
            assert line, process.stderr.read()
            message = json.loads(line)
            if 'result' in message:
                result = message['result']
                break
            request = message['request']
            requests.append(request)
            response = client.request(request['method'], request['url'], json=request['body'])
            process.stdin.write(json.dumps({'status': response.status_code, 'body': response.json()}) + '\n')
            process.stdin.flush()
        process.stdin.close()
        assert process.wait(timeout=10) == 0, process.stderr.read()
    assert client.get('/api/debt/summary', params={'customer_phone': '0944123456'}).json()['total_debt_cents'] == 30_000
    assert result['alerts'] == [], result
    assert result == {'alerts': [], 'debt': '300', 'payments': '200', 'charges': '500',
                      'summaryHidden': False, 'ledgerRows': 2}
    assert [request['method'] for request in requests] == ['POST', 'GET', 'GET']
