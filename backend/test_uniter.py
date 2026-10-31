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

def test_customer_debt_workflow():
    # 1. Create customer
    cust_res = client.post("/api/customers", json={"name": "Ahmad", "phone": "0933111222"})
    assert cust_res.status_code == 200
    cust_data = cust_res.json()
    cust_id = cust_data["id"]
    assert cust_data["name"] == "Ahmad"
    assert cust_data["debt_balance"] == 0.0

    # 2. Charge units as debt for this customer
    tx_res = client.post("/api/transactions", json={
        "operator_name": "MTN",
        "service_type": "prepaid_units",
        "phone_number": "0933111222",
        "amount": 10000,
        "customer_id": cust_id,
        "is_debt": True
    })
    assert tx_res.status_code == 200
    
    # 3. Verify customer debt updated
    custs_res = client.get("/api/customers")
    assert custs_res.status_code == 200
    customers = custs_res.json()
    ahmad = [c for c in customers if c["id"] == cust_id][0]
    assert ahmad["debt_balance"] == 10000.0

    # 4. Settle partial debt
    settle_res = client.post(f"/api/customers/{cust_id}/settle", json={"amount": 4000.0})
    assert settle_res.status_code == 200
    settled_data = settle_res.json()
    assert settled_data["debt_balance"] == 6000.0

    # 5. Check register summary
    summary_res = client.get("/api/register/summary")
    assert summary_res.status_code == 200
    summary = summary_res.json()
    assert summary["total_sales"] == 10000.0
    assert summary["total_debt"] == 6000.0
    assert summary["total_commission"] == 100.0
