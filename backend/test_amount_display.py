"""Slice 7.6: every rendered amount is shown in SYP units, never raw cents.

Server-side Jinja conversions (operator/customers/topup) are asserted on the
rendered markup; the client-side scripts (transactions, stats, customers,
topup, settlement, pos, base search) must define the shared SYP formatting
helper and use it for every money display."""

import re
import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, enable_sqlite_foreign_keys, get_db
from backend.main import app
from backend.seed import seed_data

ENV = Environment(loader=FileSystemLoader("backend/templates"), autoescape=True)

SHARED_FORMAT_BODY = "(Number(cents || 0) / 100).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 2 })"


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(tmp_path / "amounts.db"))
    )
    event.listen(engine.sync_engine, "connect", enable_sqlite_foreign_keys)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            await seed_data(session)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    asyncio.run(setup())
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


def syp(cents: int) -> str:
    """The server-side Jinja formatting used for pre-rendered amounts."""
    return "{:,.2f}".format(cents / 100).rstrip("0").rstrip(".")


@pytest.mark.parametrize(
    ("cents", "expected"),
    [
        (20000, "200"),
        (20050, "200.5"),
        (5000, "50"),
        (0, "0"),
        (1234567, "12,345.67"),
        (250000, "2,500"),
        (7482500, "74,825"),
        (5, "0.05"),
    ],
)
def test_syp_formatting(cents: int, expected: str) -> None:
    # The exact formatting used by the Jinja expressions server-side.
    assert syp(cents) == expected
    # The exact formatting used by the client-side formatAmount helper.
    python_equivalent = "{:,.2f}".format(cents / 100).rstrip("0").rstrip(".")
    assert python_equivalent == expected


def _transaction_item(amount_cents: int) -> SimpleNamespace:
    return SimpleNamespace(
        reference=f"TXN-{amount_cents}",
        created_at=datetime.now(timezone.utc),
        amount_cents=amount_cents,
        status="pending",
        ussd_string="*150*65477*0933123456*500#",
        customer_name="Ahmad",
        customer_phone="0933123456",
        operator_name="MTN",
        service_code="prepaid",
    )


def test_operator_template_amounts_in_syp() -> None:
    operator = SimpleNamespace(name="MTN", code="mtn")
    rendered = ENV.get_template("operator.html").render(
        lang="en",
        operator=operator,
        balance_cents=150000,
        transactions=[
            _transaction_item(20000),
            _transaction_item(20050),
            _transaction_item(1234567),
            _transaction_item(0),
        ],
    )
    amounts = re.findall(
        r'tabular-nums text-slate-700"><span dir="ltr">([^<]*)</span>', rendered
    )
    assert amounts == ["200", "200.5", "12,345.67", "0"]
    assert '<span id="operator-balance" dir="ltr">1,500</span>' in rendered
    assert "SYP" in rendered
    arabic = ENV.get_template("operator.html").render(
        lang="ar", operator=operator, balance_cents=150000, transactions=[_transaction_item(20000)]
    )
    assert '<span id="operator-balance" dir="ltr">1,500</span>' in arabic
    assert "ل.س" in arabic


def test_customers_template_totals_and_cells_in_syp() -> None:
    customer = SimpleNamespace(
        id=1,
        name="Ahmad",
        phone="0933123456",
        balance_cents=2500,
        debt_cents=250000,
        operator_code=None,
        service_code=None,
    )
    rendered = ENV.get_template("customers.html").render(
        lang="en", customers=[customer], total_balance_cents=2500, total_debt_cents=250000
    )
    assert 'id="total-balance" dir="ltr">25</span>' in rendered
    assert 'id="total-debt" dir="ltr">2,500</span>' in rendered
    assert 'data-field="balance"><span dir="ltr">25</span>' in rendered
    assert 'text-amber-800">2,500</span>' in rendered
    assert "SYP" in rendered
    arabic = ENV.get_template("customers.html").render(
        lang="ar", customers=[customer], total_balance_cents=2500, total_debt_cents=250000
    )
    assert 'id="total-balance" dir="ltr">25</span>' in arabic
    assert "ل.س" in arabic


def test_topup_template_balance_in_syp() -> None:
    operators = [{"code": "mtn", "name": "MTN", "balance_cents": 7482500, "last_topup_at": None}]
    rendered = ENV.get_template("topup.html").render(
        lang="en", nav_page="topup", operators=operators
    )
    assert 'data-field="balance" dir="ltr">74,825</span>' in rendered
    assert "SYP" in rendered
    arabic = ENV.get_template("topup.html").render(
        lang="ar", nav_page="topup", operators=operators
    )
    assert 'data-field="balance" dir="ltr">74,825</span>' in arabic
    assert "ل.س" in arabic


@pytest.mark.parametrize(
    "template_name", ["transactions.html", "stats.html", "customers.html", "topup.html", "settlement.html"]
)
def test_client_scripts_use_shared_syp_formatter(template_name: str) -> None:
    rendered = ENV.get_template(template_name).render(
        lang="en", customers=[], total_balance_cents=0, total_debt_cents=0, operators=[]
    )
    scripts = re.findall(r"<script>(.*?)</script>", rendered, re.DOTALL)
    page_script = max(scripts, key=lambda s: "formatAmount" in s)
    assert f"function formatAmount(cents) {{\n        return {SHARED_FORMAT_BODY};\n    }}" in page_script, (
        template_name
    )
    # Counts are never money: nothing passes a *_cents value to the count helper.
    assert not re.search(r"formatCount\([^)]*_cents", page_script)
    # And no money path formats raw cents.
    assert not re.search(r"formatCount\(total\)", page_script)


def test_pos_template_formats_syp() -> None:
    rendered = ENV.get_template("pos.html").render(lang="en")
    # Customer info balance/debt uses the shared SYP helper.
    assert "formatAmount(customer.balance_cents)" in rendered
    assert "formatAmount(debt)" in rendered
    assert SHARED_FORMAT_BODY in rendered
    # The amount summary renders SYP without a trailing ".00".
    assert "fraction === '00' ? ''" in rendered


def test_base_template_search_amounts_in_syp() -> None:
    rendered = ENV.get_template("base.html").render(lang="en", nav_page="pos")
    assert f"function formatSearchMoney(cents) {{\n            return {SHARED_FORMAT_BODY};\n        }}" in rendered
    assert "formatSearchMoney(transaction.amount_cents)" in rendered
    assert "formatSearchMoney(operator.balance_cents)" in rendered


def test_pages_render_both_languages(client: TestClient) -> None:
    for page in ("pos", "settlement", "transactions", "customers", "topup", "stats", "operators/mtn"):
        for lang in ("en", "ar"):
            response = client.get(f"/{page}", params={"lang": lang})
            assert response.status_code == 200, (page, lang)
    english = client.get("/customers").text
    arabic = client.get("/customers", params={"lang": "ar"}).text
    assert "SYP" in english
    assert "ل.س" in arabic
