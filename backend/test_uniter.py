import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, 'backend')
from main import app

client = TestClient(app)


def test_health_endpoint() -> None:
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


def test_root_endpoint() -> None:
    response = client.get('/')
    assert response.status_code == 200
    assert 'text/html' in response.headers['content-type']
    assert 'Uniter' in response.text
