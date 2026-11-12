"""Approved display-only contract: existing seed.generate_ussd, not carrier certification."""
import asyncio
from contextlib import aclosing
from decimal import Decimal
from html import unescape
import re

import pytest
from sqlalchemy import select
from backend.database import Operator, Service, get_db
from backend.main import app
from backend.seed import generate_ussd
from backend.test_settings_page import client  # noqa: F401


def patterns(html):
    return {key: unescape(re.sub('<[^>]+>', '', value)) for key, value in
            re.findall(r'<code data-ussd-pattern="([^"]+)"[^>]*>(.*?)</code>', html, re.S)}


@pytest.mark.parametrize('lang', ['en', 'ar'])
@pytest.mark.parametrize('domains', [('65477', '67858'), ('00123456789012345678', '98765432109876543210')])
@pytest.mark.parametrize('amount', [0, 5000, 12345])
def test_patterns_match_generator_and_configured_domains(client, lang, domains, amount):
    for operator, domain in zip(('mtn', 'syriatel'), domains):
        assert client.put(f'/api/settings/{operator}-domain', json={'domain': domain}).status_code == 200
        assert client.get(f'/api/settings/{operator}-domain').json()[operator + '_domain'] == domain
    html = client.get('/settings', params={'lang': lang}).text
    found = patterns(html)
    mtn, syr = domains
    assert found == {
        'mtn-prepaid': f'*150*{mtn}*{{phone}}*{{amount}}#',
        'mtn-postpaid': f'*154*{mtn}*{{phone}}*{{amount}}#',
        'syriatel-prepaid': f'*150*1*{syr}*1*{{amount}}*{{phone}}*{{phone}}#',
        'syriatel-postpaid': f'*150*1*{syr}*5*{{amount}}*{{phone}}*{{phone}}#',
    }
    async def check():
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            db = await anext(dependency)
            for key, pattern in found.items():
                operator, service = key.split('-')
                assert pattern.count('{phone}') == (1 if operator == 'mtn' else 2)
                expected = pattern.format(phone='0944123456', amount=str(amount) if operator == 'syriatel' else format(Decimal(amount) / Decimal(100), 'f'))
                assert await generate_ussd(db, operator, service, '0944123456', amount) == expected
            assert [o.ussd_prefix for o in (await db.scalars(select(Operator).order_by(Operator.code))).all()] == ['100', '555']
            assert all(s.ussd_suffix == 'amountphone#' for s in (await db.scalars(select(Service))).all())
    asyncio.run(check())
    section = html.split('id="structure-card"')[1].split('</section>')[0]
    assert '>100</td>' not in section and '>555</td>' not in section and 'amountphone#' not in section
    for marker in ['/manifest.webmanifest', '/static/install.js', 'id="install-help"', 'id="app-drawer"']:
        assert marker in html
    assert f'dir="{"rtl" if lang == "ar" else "ltr"}"' in html
