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
    response = client.get('/', follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['location'] == '/pos'
