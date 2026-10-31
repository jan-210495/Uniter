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

def test_phone_validation():
    # Invalid phone (doesn't start with 09 or wrong length)
    payload_invalid = {
        "operator_name": "MTN",
        "service_type": "prepaid_units",
        "phone_number": "0833123456",
        "amount": 5000
    }
    res = client.post("/api/transactions", json=payload_invalid)
    assert res.status_code == 422

    # Valid phone
    payload_valid = {
        "operator_name": "MTN",
        "service_type": "prepaid_units",
        "phone_number": "0944123456",
        "amount": 5000,
        "note": "Ahmad Test"
    }
    res2 = client.post("/api/transactions", json=payload_valid)
    assert res2.status_code == 200
    data = res2.json()
    assert data["phone_number"] == "0944123456"
    assert data["note"] == "Ahmad Test"
    assert data["commission"] == 50.0
    assert "*150*0944123456*5000#" in data["ussd_string"]
