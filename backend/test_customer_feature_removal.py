"""Fresh-install removal of directories/debt, without changing sale accounting."""
import pytest
from sqlalchemy import inspect

from backend import database
from backend.test_pos_customer_registration import client, commit, fund, run_db, screenshot
from backend.test_pos_ussd_result import run


@pytest.mark.parametrize("method,path", [
    ("GET", "/customers"), ("GET", "/settlement"),
    ("GET", "/api/customers"), ("POST", "/api/customers"),
    ("GET", "/api/customers/search?q=09"),
    ("GET", "/api/customers/lookup?phone=0944123456"),
    ("GET", "/api/customers/lookup-by-name?name=Ahmad"),
    ("PUT", "/api/customers/1"), ("DELETE", "/api/customers/1"),
    ("GET", "/api/customers/1/balance"),
    ("POST", "/api/customers/1/debt"),
    ("PUT", "/api/customers/1/debt/1"), ("DELETE", "/api/customers/1/debt/1"),
    ("POST", "/api/debt/charge"), ("POST", "/api/debt/payment"),
    ("GET", "/api/debt/summary"), ("GET", "/api/debt/ledger"),
])
def test_retired_routes_are_not_found(client, method, path):
    http, _ = client
    assert http.request(method, path).status_code == 404


def test_fresh_physical_schema_has_no_directory_or_debt(client):
    _, sessions = client

    async def read(session):
        connection = await session.connection()
        return await connection.run_sync(lambda sync: (
            inspect(sync).get_table_names(),
            inspect(sync).get_columns("transactions"),
            inspect(sync).get_foreign_keys("transactions"),
        ))

    tables, columns, keys = run_db(sessions, read)
    assert not {"customers", "debt_ledger"} & set(tables)
    assert not hasattr(database, "Customer")
    assert not hasattr(database, "DebtLedger")
    assert not hasattr(database.Transaction, "customer")
    assert "customer_id" not in {column["name"] for column in columns}
    assert next(column for column in columns if column["name"] == "customer_name")["nullable"]
    assert all(key["referred_table"] != "customers" for key in keys)


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_remaining_pages_have_no_directory_navigation_or_suggestions(client, lang):
    http, _ = client
    for path in ("/pos", "/transactions", "/stats", "/settings", "/operators"):
        response = http.get(path, params={"lang": lang})
        assert response.status_code == 200
        html = response.text
        assert f'<html lang="{lang}" dir="{"rtl" if lang == "ar" else "ltr"}">' in html
        for removed in ('href="/customers"', 'href="/settlement"', '/api/customers',
                        'customer-suggestions', 'customer-info', 'data.customers'):
            assert removed not in html
    assert set(http.get("/api/search", params={"q": " "}).json()) == {"transactions", "operators"}


@pytest.mark.parametrize("lang", ["en", "ar"])
@pytest.mark.parametrize("name", ["  أحمد علي  ", "   ", ""])
def test_manual_pos_records_name_or_no_name_without_customer_calls(lang, name):
    result = run([
        {"values": {"phone": "0944123456"}, "id": "phone", "event": "input"},
        {"values": {"customer-name": name}, "id": "customer-name", "event": "input"},
        {"id": "charge-form", "event": "submit"},
        {"id": "copy-ussd", "event": "click"},
    ], lang=lang)
    assert not [request for request in result["requests"] if "/api/customers" in request["url"]]
    commits = [request for request in result["requests"] if request["url"] == "/api/transactions"]
    assert len(commits) == 1
    assert commits[0]["body"]["customer_phone"] == "0944123456"
    assert commits[0]["body"]["customer_name"] == (name.strip() or None)


@pytest.mark.parametrize("name", ["  Ahmad  ", "  ", None])
def test_phone_filter_and_snapshot_search_need_only_sale_rows(client, name):
    http, sessions = client
    fund(http, amount_cents=100_000)
    sale = commit(http, phone="+963 944-123-456", name=name).json()
    normalized_name = (name or "").strip() or None
    assert sale["customer_name"] == normalized_name
    before = screenshot(sessions)
    listed = http.get("/api/transactions", params={"customer_phone": "944123"}).json()
    assert [(row["id"], row["customer_name"], row["customer_phone"]) for row in listed] == [
        (sale["id"], normalized_name, "0944123456")
    ]
    assert "customer_id" not in listed[0]
    for query in (sale["reference"], "944123", *(["ahmad"] if normalized_name else [])):
        found = http.get("/api/search", params={"q": query}).json()
        assert set(found) == {"transactions", "operators"}
        assert [(row["id"], row["customer_name"]) for row in found["transactions"]] == [
            (sale["id"], normalized_name)
        ]
    assert screenshot(sessions) == before


@pytest.mark.parametrize("field", ["customer_id", "performed_by_user_id", "seller_id"])
def test_removed_link_and_client_attribution_are_rejected(client, field):
    http, sessions = client
    fund(http)
    before = screenshot(sessions)
    response = http.post("/api/transactions", json={
        "customer_phone": "0944123456", "customer_name": "Ahmad",
        "operator_code": "mtn", "service_code": "prepaid", "amount_cents": 5000,
        "commit_key": "removed-link-rejection-0001", field: 1,
    })
    assert response.status_code == 422
    assert screenshot(sessions) == before


def test_register_reports_sales_only_without_debt_fields(client):
    http, _ = client
    fund(http, amount_cents=100_000)
    register = http.post("/api/register/open", json={"opening_balance_cents": 1000}).json()
    commit(http, name="Ahmad", amount_cents=5000)
    failed = commit(http, amount_cents=2000).json()
    assert http.patch(f"/api/transactions/{failed['id']}/status", json={"status": "failed"}).status_code == 200
    summary = http.get("/api/register/summary").json()
    assert summary == {
        "opening_balance_cents": 1000,
        "cash_collected_cents": 5000,
        "closing_balance_cents": None,
        "transaction_count": 1,
    }
    closed = http.post("/api/register/close", params={"register_id": register["id"]})
    assert closed.status_code == 200
    assert closed.json()["closing_balance_cents"] == 6000
    assert http.get("/api/register/summary").json() == {**summary, "closing_balance_cents": 6000}
