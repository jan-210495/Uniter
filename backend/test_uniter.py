import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os

from backend.main import app, get_db
from backend.database import Base, init_db

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_uniter.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    init_db(engine)
    yield
    Base.metadata.drop_all(bind=engine)

client = TestClient(app)

def test_health_check():
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "MTN" in data["operators"]
    assert "Syriatel" in data["operators"]

def test_get_operators_and_services():
    res_op = client.get("/api/operators")
    assert res_op.status_code == 200
    ops = res_op.json()
    assert len(ops) >= 2

    res_srv = client.get("/api/services")
    assert res_srv.status_code == 200
    srvs = res_srv.json()
    assert len(srvs) >= 4

def test_create_transaction_and_ussd():
    payload = {
        "operator_name": "MTN",
        "service_type": "prepaid_units",
        "phone_number": "0933123456",
        "amount": 5000
    }
    response = client.post("/api/transactions", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["operator_name"] == "MTN"
    assert data["phone_number"] == "0933123456"
    assert data["amount"] == 5000
    assert "*150*" in data["ussd_string"]
    assert "0933123456" in data["ussd_string"]
