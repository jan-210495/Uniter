"""Test support for v1.0.4 Slice 4.1: fund a SIM card and sell from it.

Since Slice 4.1 every new sale is charged to a selected SIM card; the legacy
company shop credit (``operator_balance``) is historical and read-only. Tests
that exercise the sale flow therefore create a SIM card, top it up through the
real audited endpoint and pass its id with each preview/commit, instead of
loading the old company credit. Every call goes through the authenticated,
CSRF-protected API exactly like the browser does.
"""
from __future__ import annotations

import itertools

from fastapi.testclient import TestClient

_KEYS = itertools.count(1)
#: A distinct, valid Syrian number per SIM card created by the helpers.
_PHONES = itertools.count(1)

#: Operators page / API field names, kept in one place.
TOPUP_URL = "/api/sims/{sim_id}/topup"
#: The two companies that have SIM cards.
COMPANIES = ("mtn", "syriatel")


def new_key(prefix: str = "simtestkey") -> str:
    """A fresh idempotency key (top-up or commit), valid for the API pattern."""
    return f"{prefix}{next(_KEYS):020d}"


def create_sim(
    client: TestClient,
    operator_code: str = "mtn",
    *,
    visibility: str = "shared",
    display_name: str | None = None,
    phone: str | None = None,
    owner_user_id: int | None = None,
) -> dict:
    body = {
        "operator_code": operator_code,
        "display_name": display_name or f"{operator_code.upper()} test SIM",
        "phone": phone or f"0944{next(_PHONES) % 1_000_000:06d}",
        "visibility": visibility,
    }
    if owner_user_id is not None:
        body["owner_user_id"] = owner_user_id
    response = client.post("/api/sims", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def topup_sim(client: TestClient, sim_id: int, amount_cents: int, key: str | None = None):
    return client.post(
        TOPUP_URL.format(sim_id=sim_id),
        json={"amount_cents": amount_cents, "topup_key": key or new_key("topupkey")},
    )


def fund_sim(
    client: TestClient, operator_code: str = "mtn", amount_cents: int = 5_000_000, **sim_options
) -> int:
    """Create a SIM card of ``operator_code``, top it up and return its id.

    The id is also remembered on the client (``client.sim_ids[operator_code]``)
    so sale helpers can find the SIM of a company without threading it through.
    """
    sim = create_sim(client, operator_code, **sim_options)
    if amount_cents:
        response = topup_sim(client, sim["id"], amount_cents)
        assert response.status_code == 200, response.text
    remember_sim(client, operator_code, sim["id"])
    return sim["id"]


def fund_company(client: TestClient, operator_code: str = "mtn", amount_cents: int = 5_000_000) -> int:
    """Top up this client's SIM card of ``operator_code`` (created on first use).

    The Slice 4.1 replacement for loading the legacy company credit: one shared
    SIM card per company, funded through the audited per-SIM top-up, so repeated
    funding accumulates on one balance exactly as the old company credit did.
    """
    sim_ids = getattr(client, "sim_ids", {})
    if operator_code not in sim_ids:
        return fund_sim(client, operator_code, amount_cents)
    response = topup_sim(client, sim_ids[operator_code], amount_cents)
    assert response.status_code == 200, response.text
    return sim_ids[operator_code]


def sim_balances_by_operator(session_rows) -> dict:
    """``{operator_code: summed SIM balance}`` from (code, balance) rows."""
    totals: dict = {}
    for code, balance in session_rows:
        totals[code] = totals.get(code, 0) + (balance or 0)
    return totals


def remember_sim(client: TestClient, operator_code: str, sim_id: int) -> None:
    if not hasattr(client, "sim_ids"):
        client.sim_ids = {}
    client.sim_ids[operator_code] = sim_id


def sim_for(client: TestClient, operator_code: str) -> int:
    """The SIM card of ``operator_code`` funded on this client (created empty if none)."""
    sim_ids = getattr(client, "sim_ids", {})
    if operator_code not in sim_ids:
        return fund_sim(client, operator_code, 0)
    return sim_ids[operator_code]


def with_sim(client: TestClient, payload: dict) -> dict:
    """``payload`` plus the remembered SIM of its company (unless one is set)."""
    if "sim_card_id" in payload or payload.get("operator_code") not in COMPANIES:
        return payload  # an unknown company is refused before any SIM check
    return {**payload, "sim_card_id": sim_for(client, payload["operator_code"])}


def sim_balance(client: TestClient, sim_id: int) -> int:
    response = client.get(f"/api/sims/{sim_id}")
    assert response.status_code == 200, response.text
    return response.json()["balance_cents"]


def sim_total_statement(operator_code: str):
    """SELECT the summed SIM card balance of ``operator_code`` (0 without SIMs).

    The Slice 4.1 counterpart of reading the company's ``operator_balance``:
    the money a sale debits and a failed-sale refund returns now lives here.
    """
    from sqlalchemy import func, select

    try:
        from .database import Operator, SimBalance, SimCard
    except ImportError:  # pragma: no cover - run from backend/
        from database import Operator, SimBalance, SimCard

    return (
        select(func.coalesce(func.sum(SimBalance.balance_cents), 0))
        .select_from(Operator)
        .join(SimCard, SimCard.operator_id == Operator.id)
        .join(SimBalance, SimBalance.sim_card_id == SimCard.id)
        .where(Operator.code == operator_code)
    )


def add_legacy_credit(operator_code: str, amount_cents: int) -> None:
    """Historical company credit, as a pre-4.1 database holds it.

    Since Slice 4.1 the legacy credit cannot be topped up through the API, so
    tests that show how it is displayed write it the way an upgraded database
    already contains it (through the app's own test session override).
    """
    import asyncio
    from contextlib import aclosing

    from sqlalchemy import select

    from backend.database import Operator, OperatorBalance, get_db
    from backend.main import app

    async def write() -> None:
        async with aclosing(app.dependency_overrides[get_db]()) as dependency:
            session = await anext(dependency)
            operator = await session.scalar(select(Operator).where(Operator.code == operator_code))
            balance = await session.scalar(
                select(OperatorBalance).where(OperatorBalance.operator_id == operator.id)
            )
            if balance is None:
                session.add(OperatorBalance(operator_id=operator.id, balance_cents=amount_cents))
            else:
                balance.balance_cents += amount_cents
            await session.commit()

    asyncio.run(write())
