from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Dict
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, func, or_, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.selectable import Subquery
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

if __package__:
    from .commission import get_commission_for_transaction
    from .database import Customer, Operator, Service, Transaction, DebtLedger, OperatorBalance, OperatorTopup, ProfitLedger, CashRegister, CurrencyDenomination, get_db, get_setting, set_setting
    from .seed import DEFAULT_MTN_DOMAIN, DEFAULT_SYRIATEL_DOMAIN, generate_ussd
    from .validation import validate_syrian_phone
else:
    from commission import get_commission_for_transaction
    from database import Customer, Operator, Service, Transaction, DebtLedger, OperatorBalance, OperatorTopup, ProfitLedger, CashRegister, CurrencyDenomination, get_db, get_setting, set_setting
    from seed import DEFAULT_MTN_DOMAIN, DEFAULT_SYRIATEL_DOMAIN, generate_ussd
    from validation import validate_syrian_phone

app = FastAPI(title="Uniter")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

Code = Annotated[str, Field(min_length=1, max_length=50)]
DbSession = Annotated[AsyncSession, Depends(get_db)]

# Display timezone: the stored default and the options offered on /settings.
DEFAULT_TIMEZONE = "Asia/Damascus"
TIMEZONE_OPTIONS = (
    "UTC",
    "Asia/Damascus",
    "Europe/Damascus",
    "Europe/Zurich",
    "Europe/London",
    "America/New_York",
    "Asia/Dubai",
    "Asia/Riyadh",
)


class CustomerCreate(BaseModel):
    phone: str
    name: str = Field(min_length=1, max_length=255)
    operator_code: str | None = Field(default=None, min_length=1, max_length=50)
    service_code: str | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, phone: str) -> str:
        return validate_syrian_phone(phone)


class CustomerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    phone: str
    name: str
    balance_cents: int


class CustomerUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Customer name cannot be empty")
        return cleaned


class CustomerListItem(CustomerResponse):
    """A customer plus the debt balance aggregated from the debt ledger."""

    debt_cents: int


class CustomerDebtCreate(BaseModel):
    """A debt entry created against a customer id (no phone lookup needed)."""

    amount_cents: int = Field(ge=0, le=9223372036854775807)
    description: str = Field(default="", max_length=512)
    type: str = Field(default="charge", pattern="^(charge|payment)$")


class CustomerDebtUpdate(BaseModel):
    """Partial update of an existing debt entry."""

    amount_cents: int | None = Field(default=None, ge=0, le=9223372036854775807)
    description: str | None = Field(default=None, max_length=512)
    type: str | None = Field(default=None, pattern="^(charge|payment)$")


class CustomerBalanceResponse(BaseModel):
    total_debt_cents: int
    total_charges_cents: int
    total_payments_cents: int


class OperatorBalanceResponse(BaseModel):
    operator_code: str
    operator_name: str
    balance_cents: int


class OperatorTopupCreate(BaseModel):
    operator_code: Code
    amount_cents: int = Field(gt=0, le=9223372036854775807)


class OperatorTopupResponse(BaseModel):
    operator_code: str
    amount_cents: int
    new_balance_cents: int
    reference: str


class TransactionCreate(BaseModel):
    customer_phone: str
    operator_code: Code
    service_code: Code
    amount_cents: int = Field(ge=0, le=9223372036854775807)

    @field_validator("customer_phone")
    @classmethod
    def validate_customer_phone(cls, phone: str) -> str:
        return validate_syrian_phone(phone)


class CommissionSettingUpdate(BaseModel):
    rate: Decimal = Field(ge=0, le=1, allow_inf_nan=False)


class DomainSettingUpdate(BaseModel):
    """USSD domain code: non-empty digits only, at most 20 characters."""

    domain: str = Field(min_length=1, max_length=20, pattern=r"^\d+$")


class TimezoneSettingUpdate(BaseModel):
    """Display timezone: a non-empty zone name, at most 64 characters."""

    timezone: str = Field(min_length=1, max_length=64)


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    ussd_string: str
    status: str
    amount_cents: int
    created_at: datetime


class TransactionListItem(BaseModel):
    """A transaction enriched with customer, operator and service labels."""

    id: int
    reference: str
    ussd_string: str
    status: str
    amount_cents: int
    created_at: datetime
    customer_phone: str
    customer_name: str
    operator_code: str
    operator_name: str
    service_code: str
    service_name: str


class SearchTransactionItem(BaseModel):
    """Transaction fields shown in global search results."""

    id: int
    reference: str
    amount_cents: int
    status: str
    customer_phone: str
    customer_name: str
    operator_code: str
    operator_name: str


class SearchOperatorItem(BaseModel):
    """Operator fields shown in global search results."""

    code: str
    name: str
    balance_cents: int


class SearchResults(BaseModel):
    """Global search response: customers, transactions and operators."""

    customers: list[CustomerListItem]
    transactions: list[SearchTransactionItem]
    operators: list[SearchOperatorItem]


class StatsTotals(BaseModel):
    """Window-wide profit and transaction-volume totals."""

    profit_cents: int
    commission_cents: int
    volume_cents: int
    transaction_count: int


class StatsOperatorRow(BaseModel):
    """Per-operator profit and transaction volume inside the stats window."""

    operator_code: str
    operator_name: str
    profit_cents: int
    commission_cents: int
    volume_cents: int
    transaction_count: int


class StatsStatusRow(BaseModel):
    """Transaction count and volume for one status value."""

    status: str
    count: int
    volume_cents: int


class StatsDailyRow(BaseModel):
    """One calendar day of the daily series (zero-filled)."""

    date: str
    profit_cents: int
    volume_cents: int
    transaction_count: int


class StatsSummary(BaseModel):
    """Response of GET /api/stats/summary."""

    period_days: int
    operator_code: str | None
    totals: StatsTotals
    by_operator: list[StatsOperatorRow]
    by_status: list[StatsStatusRow]
    daily: list[StatsDailyRow]


class DebtLedgerCreate(BaseModel):
    customer_phone: str
    amount_cents: int = Field(ge=0)
    description: str = Field(default="", max_length=512)
    type: str = Field(default="charge", pattern="^(charge|payment)$")

    @field_validator("customer_phone")
    @classmethod
    def validate_customer_phone(cls, phone: str) -> str:
        return validate_syrian_phone(phone)


class DebtLedgerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_phone: str
    amount_cents: int
    description: str
    type: str
    created_at: datetime


# --- Cash Register Models ---


class DebtPaymentCreate(BaseModel):
    customer_phone: str
    amount_cents: int = Field(ge=0)
    description: str = Field(default="")

    @field_validator("customer_phone")
    @classmethod
    def validate_customer_phone(cls, phone: str) -> str:
        return validate_syrian_phone(phone)


class CashRegisterCreate(BaseModel):
    opening_balance_cents: int = Field(ge=0, default=0)


class CashRegisterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    opening_balance_cents: int
    closing_balance_cents: int | None = None
    status: str
    opened_at: datetime
    closed_at: datetime | None = None


class RegisterSummary(BaseModel):
    opening_balance_cents: int
    cash_collected_cents: int
    debt_collected_cents: int
    closing_balance_cents: int | None
    transaction_count: int
    debt_payment_count: int
class CurrencyDenominationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scale_name: str
    multiplier: int
    is_active: bool


class SetDenomination(BaseModel):
    scale_name: str



@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    return HTMLResponse(
        "<h1>Uniter — Mobile Shop Charging</h1>"
        "<p>Bilingual MTN/Syriatel prepaid & postpaid management.</p>"
    )


@app.get("/pos", response_class=HTMLResponse)
async def pos_page(request: Request, db: DbSession, lang: str = "en") -> HTMLResponse:
    language = lang if lang in {"en", "ar"} else "en"
    denom = await db.scalar(select(CurrencyDenomination).where(CurrencyDenomination.is_active == True))
    return templates.TemplateResponse(request=request, name="pos.html", context={"lang": language, "currency_denomination": denom})

@app.get("/settlement", response_class=HTMLResponse)
async def settlement_page(request: Request, db: DbSession, lang: str = "en") -> HTMLResponse:
    language = lang if lang in {"en", "ar"} else "en"
    denom = await db.scalar(select(CurrencyDenomination).where(CurrencyDenomination.is_active == True))
    return templates.TemplateResponse(request=request, name="settlement.html", context={"lang": language, "currency_denomination": denom})


@app.get("/transactions", response_class=HTMLResponse)
async def transactions_page(request: Request, lang: str = "en") -> HTMLResponse:
    language = lang if lang in {"en", "ar"} else "en"
    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context={"lang": language, "nav_page": "transactions"},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: DbSession, lang: str = "en") -> HTMLResponse:
    """Settings page: commission rate, active currency denomination and the
    read-only operator/service code structure, rendered server-side."""
    language = lang if lang in {"en", "ar"} else "en"
    operators = (
        await db.scalars(
            select(Operator).options(selectinload(Operator.services)).order_by(Operator.code)
        )
    ).all()
    denominations = (
        await db.scalars(select(CurrencyDenomination).order_by(CurrencyDenomination.id))
    ).all()
    active_denomination = next((d for d in denominations if d.is_active), None)
    mtn_domain = await get_setting(db, "MTN_DOMAIN", DEFAULT_MTN_DOMAIN)
    syriatel_domain = await get_setting(db, "SYRIATEL_DOMAIN", DEFAULT_SYRIATEL_DOMAIN)
    timezone = await get_setting(db, "timezone", DEFAULT_TIMEZONE)
    assert mtn_domain is not None and syriatel_domain is not None and timezone is not None
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "lang": language,
            "nav_page": "settings",
            "operators": operators,
            "denominations": denominations,
            "active_denomination": active_denomination,
            "mtn_domain": mtn_domain,
            "syriatel_domain": syriatel_domain,
            "timezones": TIMEZONE_OPTIONS,
            "timezone": timezone,
        },
    )


@app.get("/topup", response_class=HTMLResponse)
async def topup_page(request: Request, db: DbSession, lang: str = "en") -> HTMLResponse:
    """Balance topup page: per-operator remaining credit, last topup and form."""
    language = lang if lang in {"en", "ar"} else "en"
    rows = (
        await db.execute(
            select(
                Operator.code,
                Operator.name,
                func.coalesce(OperatorBalance.balance_cents, 0).label("balance_cents"),
                func.max(OperatorTopup.created_at).label("last_topup_at"),
            )
            .outerjoin(OperatorBalance, OperatorBalance.operator_id == Operator.id)
            .outerjoin(OperatorTopup, OperatorTopup.operator_id == Operator.id)
            .group_by(Operator.id)
            .order_by(Operator.code)
        )
    ).all()
    operators = [
        {
            "code": row.code,
            "name": row.name,
            "balance_cents": int(row.balance_cents or 0),
            "last_topup_at": row.last_topup_at,
        }
        for row in rows
    ]
    return templates.TemplateResponse(
        request=request,
        name="topup.html",
        context={"lang": language, "nav_page": "topup", "operators": operators},
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, lang: str = "en") -> HTMLResponse:
    """Stats dashboard: profit and transaction volume over time."""
    language = lang if lang in {"en", "ar"} else "en"
    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context={"lang": language, "nav_page": "stats"},
    )


@app.get("/operators/{operator_code}", response_class=HTMLResponse)
async def operator_page(
    request: Request, db: DbSession, operator_code: str, lang: str = "en"
) -> HTMLResponse:
    """Operator subpage: balance, latest transactions and a top-up link."""
    language = lang if lang in {"en", "ar"} else "en"
    operator = await db.scalar(select(Operator).where(Operator.code == operator_code))
    if operator is None or not operator.is_active:
        raise HTTPException(status_code=404, detail="Operator not found")
    balance = await db.scalar(
        select(OperatorBalance.balance_cents).where(OperatorBalance.operator_id == operator.id)
    )
    rows = await db.execute(
        _transaction_list_statement()
        .where(Operator.code == operator_code)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(20)
    )
    transactions = [TransactionListItem(**row._mapping) for row in rows.all()]
    return templates.TemplateResponse(
        request=request,
        name="operator.html",
        context={
            "lang": language,
            "operator": operator,
            "balance_cents": int(balance or 0),
            "transactions": transactions,
        },
    )


@app.get("/api/search", response_model=SearchResults)
async def global_search(
    db: DbSession,
    q: Annotated[str | None, Query(max_length=255)] = None,
) -> SearchResults:
    """Global search across customers, transactions and operators.

    Matches customers by name or phone fragment, transactions by reference or
    customer phone fragment, and operators by code or name fragment. Each
    section is limited to 10 results; an empty query returns empty sections.
    """
    query = (q or "").strip()
    if not query:
        return SearchResults(customers=[], transactions=[], operators=[])
    like = f"%{query}%"

    debt = _debt_totals_by_customer()
    customer_rows = await db.execute(
        select(
            Customer.id,
            Customer.phone,
            Customer.name,
            Customer.balance_cents,
            func.coalesce(debt.c.debt_cents, 0).label("debt_cents"),
        )
        .outerjoin(debt, debt.c.customer_id == Customer.id)
        .where(or_(Customer.phone.ilike(like), Customer.name.ilike(like)))
        .order_by(Customer.name, Customer.id)
        .limit(10)
    )
    customers = [CustomerListItem(**row._mapping) for row in customer_rows.all()]

    transaction_rows = await db.execute(
        select(
            Transaction.id,
            Transaction.reference,
            Transaction.amount_cents,
            Transaction.status,
            Customer.phone.label("customer_phone"),
            Customer.name.label("customer_name"),
            Operator.code.label("operator_code"),
            Operator.name.label("operator_name"),
        )
        .join(Customer, Transaction.customer_id == Customer.id)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
        .where(or_(Transaction.reference.ilike(like), Customer.phone.ilike(like)))
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(10)
    )
    transactions = [SearchTransactionItem(**row._mapping) for row in transaction_rows.all()]

    operator_rows = await db.execute(
        select(
            Operator.code,
            Operator.name,
            func.coalesce(OperatorBalance.balance_cents, 0).label("balance_cents"),
        )
        .outerjoin(OperatorBalance, OperatorBalance.operator_id == Operator.id)
        .where(or_(Operator.code.ilike(like), Operator.name.ilike(like)))
        .order_by(Operator.code)
        .limit(10)
    )
    operators = [SearchOperatorItem(**row._mapping) for row in operator_rows.all()]

    return SearchResults(customers=customers, transactions=transactions, operators=operators)


@app.get("/api/stats/summary", response_model=StatsSummary)
async def stats_summary(
    db: DbSession,
    period: Annotated[int, Query(ge=1, le=90)] = 7,
    operator_code: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
) -> StatsSummary:
    """Profit and transaction-volume statistics over the last ``period`` days.

    Profit and commission come from the ProfitLedger, volume and counts from
    Transaction; both cover the same UTC window and are optionally restricted
    to one operator. The daily series is zero-filled so every calendar day in
    the window has a row, oldest first.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=period)

    profit_conditions = [ProfitLedger.created_at >= window_start]
    transaction_conditions = [Transaction.created_at >= window_start]
    if operator_code is not None:
        profit_conditions.append(ProfitLedger.operator_code == operator_code)
        transaction_conditions.append(Operator.code == operator_code)

    # Profit per operator (the ledger carries the operator code directly).
    profit_rows = await db.execute(
        select(
            ProfitLedger.operator_code,
            func.coalesce(func.sum(ProfitLedger.profit_cents), 0).label("profit_cents"),
            func.coalesce(func.sum(ProfitLedger.commission_cents), 0).label("commission_cents"),
        )
        .where(*profit_conditions)
        .group_by(ProfitLedger.operator_code)
    )
    profit_by_operator = {
        row.operator_code: (int(row.profit_cents), int(row.commission_cents))
        for row in profit_rows.all()
    }

    # Volume and count per operator; transactions reach the operator via services.
    volume_rows = await db.execute(
        select(
            Operator.code,
            func.count(Transaction.id).label("transaction_count"),
            func.coalesce(func.sum(Transaction.amount_cents), 0).label("volume_cents"),
        )
        .select_from(Transaction)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
        .where(*transaction_conditions)
        .group_by(Operator.code)
    )
    volume_by_operator = {
        row.code: (int(row.transaction_count), int(row.volume_cents)) for row in volume_rows.all()
    }

    status_rows = await db.execute(
        select(
            Transaction.status,
            func.count(Transaction.id).label("count"),
            func.coalesce(func.sum(Transaction.amount_cents), 0).label("volume_cents"),
        )
        .select_from(Transaction)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
        .where(*transaction_conditions)
        .group_by(Transaction.status)
    )
    status_counts = {row.status: (int(row.count), int(row.volume_cents)) for row in status_rows.all()}

    profit_day_rows = await db.execute(
        select(
            func.date(ProfitLedger.created_at).label("day"),
            func.coalesce(func.sum(ProfitLedger.profit_cents), 0).label("profit_cents"),
        )
        .where(*profit_conditions)
        .group_by(func.date(ProfitLedger.created_at))
    )
    profit_by_day = {str(row.day): int(row.profit_cents) for row in profit_day_rows.all()}

    volume_day_rows = await db.execute(
        select(
            func.date(Transaction.created_at).label("day"),
            func.count(Transaction.id).label("transaction_count"),
            func.coalesce(func.sum(Transaction.amount_cents), 0).label("volume_cents"),
        )
        .select_from(Transaction)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
        .where(*transaction_conditions)
        .group_by(func.date(Transaction.created_at))
    )
    volume_by_day = {
        str(row.day): (int(row.transaction_count), int(row.volume_cents))
        for row in volume_day_rows.all()
    }

    # Every operator appears in the breakdown, zeroed when idle inside the window.
    operator_statement = select(Operator.code, Operator.name).order_by(Operator.code)
    if operator_code is not None:
        operator_statement = operator_statement.where(Operator.code == operator_code)
    operator_rows = await db.execute(operator_statement)
    by_operator = [
        StatsOperatorRow(
            operator_code=row.code,
            operator_name=row.name,
            profit_cents=profit_by_operator.get(row.code, (0, 0))[0],
            commission_cents=profit_by_operator.get(row.code, (0, 0))[1],
            volume_cents=volume_by_operator.get(row.code, (0, 0))[1],
            transaction_count=volume_by_operator.get(row.code, (0, 0))[0],
        )
        for row in operator_rows.all()
    ]
    by_operator.sort(key=lambda item: (-item.profit_cents, item.operator_code))

    totals = StatsTotals(
        profit_cents=sum(profit for profit, _ in profit_by_operator.values()),
        commission_cents=sum(commission for _, commission in profit_by_operator.values()),
        volume_cents=sum(volume for _, volume in volume_by_operator.values()),
        transaction_count=sum(count for count, _ in volume_by_operator.values()),
    )
    by_status = [
        StatsStatusRow(
            status=status,
            count=status_counts.get(status, (0, 0))[0],
            volume_cents=status_counts.get(status, (0, 0))[1],
        )
        for status in ("pending", "completed", "failed")
    ]

    today = now.date()
    daily: list[StatsDailyRow] = []
    for offset in range(period - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        count, volume = volume_by_day.get(day, (0, 0))
        daily.append(
            StatsDailyRow(
                date=day,
                profit_cents=profit_by_day.get(day, 0),
                volume_cents=volume,
                transaction_count=count,
            )
        )

    return StatsSummary(
        period_days=period,
        operator_code=operator_code,
        totals=totals,
        by_operator=by_operator,
        by_status=by_status,
        daily=daily,
    )


def _debt_totals_by_customer() -> Subquery:
    """Aggregate DebtLedger into (customer_id, charges - payments) per customer."""
    charges = func.sum(case((DebtLedger.type == "charge", DebtLedger.amount_cents), else_=0))
    payments = func.sum(case((DebtLedger.type == "payment", DebtLedger.amount_cents), else_=0))
    return (
        select(
            DebtLedger.customer_id.label("customer_id"),
            (charges - payments).label("debt_cents"),
        )
        .group_by(DebtLedger.customer_id)
        .subquery()
    )


async def list_customers_with_debt(db: AsyncSession) -> list[CustomerListItem]:
    """Return every customer with its debt balance (charges minus payments)."""
    debt = _debt_totals_by_customer()
    rows = await db.execute(
        select(
            Customer.id,
            Customer.phone,
            Customer.name,
            Customer.balance_cents,
            func.coalesce(debt.c.debt_cents, 0).label("debt_cents"),
        )
        .outerjoin(debt, debt.c.customer_id == Customer.id)
        .order_by(Customer.name, Customer.id)
    )
    return [
        CustomerListItem(
            id=row.id,
            phone=row.phone,
            name=row.name,
            balance_cents=row.balance_cents,
            debt_cents=int(row.debt_cents or 0),
        )
        for row in rows.all()
    ]


@app.get("/customers", response_class=HTMLResponse)
async def customers_page(request: Request, db: DbSession, lang: str = "en") -> HTMLResponse:
    language = lang if lang in {"en", "ar"} else "en"
    customers = await list_customers_with_debt(db)
    return templates.TemplateResponse(
        request=request,
        name="customers.html",
        context={
            "lang": language,
            "nav_page": "customers",
            "customers": customers,
            "total_debt_cents": sum(customer.debt_cents for customer in customers),
            "total_balance_cents": sum(customer.balance_cents for customer in customers),
        },
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/settings/commission")
async def read_commission_setting(db: DbSession) -> dict[str, str]:
    rate = await get_setting(db, "commission_rate", "0.05")
    assert rate is not None
    return {"commission_rate": rate}


@app.put("/api/settings/commission")
async def update_commission_setting(data: CommissionSettingUpdate, db: DbSession) -> dict[str, str]:
    setting = await set_setting(db, "commission_rate", str(data.rate))
    value = setting.value
    await db.commit()
    return {"commission_rate": value}


@app.get("/api/settings/mtn-domain")
async def read_mtn_domain_setting(db: DbSession) -> dict[str, str]:
    """Current MTN USSD domain code (default 65477 when not stored)."""
    domain = await get_setting(db, "MTN_DOMAIN", DEFAULT_MTN_DOMAIN)
    assert domain is not None
    return {"mtn_domain": domain}


@app.put("/api/settings/mtn-domain")
async def update_mtn_domain_setting(data: DomainSettingUpdate, db: DbSession) -> dict[str, str]:
    """Store the MTN USSD domain code consumed by generate_ussd."""
    setting = await set_setting(db, "MTN_DOMAIN", data.domain)
    value = setting.value
    await db.commit()
    return {"mtn_domain": value}


@app.get("/api/settings/syriatel-domain")
async def read_syriatel_domain_setting(db: DbSession) -> dict[str, str]:
    """Current Syriatel USSD domain code (default 67858 when not stored)."""
    domain = await get_setting(db, "SYRIATEL_DOMAIN", DEFAULT_SYRIATEL_DOMAIN)
    assert domain is not None
    return {"syriatel_domain": domain}


@app.put("/api/settings/syriatel-domain")
async def update_syriatel_domain_setting(data: DomainSettingUpdate, db: DbSession) -> dict[str, str]:
    """Store the Syriatel USSD domain code consumed by generate_ussd."""
    setting = await set_setting(db, "SYRIATEL_DOMAIN", data.domain)
    value = setting.value
    await db.commit()
    return {"syriatel_domain": value}


@app.get("/api/settings/timezone")
async def read_timezone_setting(db: DbSession) -> dict[str, str]:
    """Current display timezone (default Asia/Damascus when not stored)."""
    timezone = await get_setting(db, "timezone", DEFAULT_TIMEZONE)
    assert timezone is not None
    return {"timezone": timezone}


@app.put("/api/settings/timezone")
async def update_timezone_setting(data: TimezoneSettingUpdate, db: DbSession) -> dict[str, str]:
    """Store the display timezone shown in the header and used for times."""
    setting = await set_setting(db, "timezone", data.timezone)
    value = setting.value
    await db.commit()
    return {"timezone": value}


@app.get("/api/commission/calculate")
async def commission_calculation(
    db: DbSession,
    amount_cents: Annotated[int, Query(ge=0, le=9223372036854775807)],
    operator_code: Annotated[str, Query(min_length=1, max_length=50)],
    service_code: Annotated[str, Query(min_length=1, max_length=50)],
) -> dict[str, int | str]:
    operator = await db.scalar(select(Operator).where(Operator.code == operator_code))
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator not found")
    service = await db.scalar(
        select(Service).where(Service.operator_id == operator.id, Service.code == service_code)
    )
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found for operator")
    try:
        return await get_commission_for_transaction(db, amount_cents)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid stored commission rate") from exc


@app.get("/api/customers/lookup", response_model=CustomerResponse)
async def lookup_customer(
    db: DbSession,
    phone: Annotated[str, Query()],
) -> Customer:
    try:
        phone = validate_syrian_phone(phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    customer = await db.scalar(select(Customer).where(Customer.phone == phone))
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@app.get("/api/customers/lookup-by-name", response_model=CustomerResponse)
async def lookup_customer_by_name(
    db: DbSession,
    name: Annotated[str, Query(min_length=1, max_length=255)],
) -> Customer:
    """Exact (case-insensitive) name match for POS auto-recognition."""
    customer = await db.scalar(
        select(Customer)
        .where(func.lower(Customer.name) == name.strip().lower())
        .order_by(Customer.id)
        .limit(1)
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@app.post("/api/customers", response_model=CustomerResponse)
async def create_customer(data: CustomerCreate, db: DbSession) -> Customer:
    operator = None
    if data.operator_code is not None:
        operator = await db.scalar(select(Operator).where(Operator.code == data.operator_code))
        if operator is None:
            raise HTTPException(status_code=422, detail="Unknown operator code")
    if data.service_code is not None:
        service_statement = select(Service).where(Service.code == data.service_code)
        if operator is not None:
            # The service must belong to the given operator when both are provided.
            service_statement = service_statement.where(Service.operator_id == operator.id)
        service = await db.scalar(service_statement.limit(1))
        if service is None:
            raise HTTPException(status_code=422, detail="Unknown service code")
    customer = Customer(
        phone=data.phone,
        name=data.name,
        operator_code=data.operator_code,
        service_code=data.service_code,
    )
    db.add(customer)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(select(Customer).where(Customer.phone == data.phone))
        if existing is not None:
            raise HTTPException(status_code=409, detail="Customer phone already exists")
        raise
    await db.refresh(customer)
    return customer


@app.get("/api/customers", response_model=list[CustomerListItem])
async def list_customers(db: DbSession) -> list[CustomerListItem]:
    """List every customer with its aggregated debt balance."""
    return await list_customers_with_debt(db)


@app.get("/api/customers/search", response_model=list[CustomerListItem])
async def search_customers(
    q: Annotated[str, Query(min_length=1, max_length=255)],
    db: DbSession,
) -> list[CustomerListItem]:
    """Search customers by phone or name fragment for POS recognition."""
    debt = _debt_totals_by_customer()
    like = f"%{q}%"
    rows = await db.execute(
        select(
            Customer.id,
            Customer.phone,
            Customer.name,
            Customer.balance_cents,
            func.coalesce(debt.c.debt_cents, 0).label("debt_cents"),
        )
        .outerjoin(debt, debt.c.customer_id == Customer.id)
        .where(Customer.phone.ilike(like) | Customer.name.ilike(like))
        .order_by(Customer.name, Customer.id)
        .limit(20)
    )
    return [
        CustomerListItem(
            id=row.id,
            phone=row.phone,
            name=row.name,
            balance_cents=row.balance_cents,
            debt_cents=int(row.debt_cents or 0),
        )
        for row in rows.all()
    ]


@app.put("/api/customers/{customer_id}", response_model=CustomerResponse)
async def update_customer(customer_id: int, data: CustomerUpdate, db: DbSession) -> Customer:
    """Rename a customer. Phone numbers are the ledger key and stay immutable."""
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    customer.name = data.name
    await db.commit()
    await db.refresh(customer)
    return customer


@app.delete("/api/customers/{customer_id}", status_code=204, response_class=Response)
async def delete_customer(customer_id: int, db: DbSession) -> Response:
    """Remove a customer together with its transactions and debt ledger rows."""
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    # Dependent rows are removed explicitly: SQLite enforces the foreign keys,
    # and the customer row owns this history once the customer is deleted.
    customer_transactions = select(Transaction.id).where(Transaction.customer_id == customer_id)
    await db.execute(
        sql_delete(ProfitLedger).where(ProfitLedger.transaction_id.in_(customer_transactions))
    )
    await db.execute(sql_delete(DebtLedger).where(DebtLedger.customer_id == customer_id))
    await db.execute(sql_delete(Transaction).where(Transaction.customer_id == customer_id))
    await db.delete(customer)
    await db.commit()
    return Response(status_code=204)


# --- Customer-scoped debt management ---


def _debt_entry_response(entry: DebtLedger, customer: Customer) -> DebtLedgerResponse:
    return DebtLedgerResponse(
        id=entry.id,
        customer_phone=customer.phone,
        amount_cents=entry.amount_cents,
        description=entry.description,
        type=entry.type,
        created_at=entry.created_at,
    )


async def _debt_totals_for(db: AsyncSession, customer_id: int) -> tuple[int, int]:
    """Return (total_charges_cents, total_payments_cents) for one customer."""
    row = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(case((DebtLedger.type == "charge", DebtLedger.amount_cents), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((DebtLedger.type == "payment", DebtLedger.amount_cents), else_=0)),
                    0,
                ),
            ).where(DebtLedger.customer_id == customer_id)
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


async def _get_customer_or_404(db: AsyncSession, customer_id: int) -> Customer:
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


async def _get_debt_entry_or_404(
    db: AsyncSession, customer_id: int, debt_id: int
) -> DebtLedger:
    entry = await db.scalar(
        select(DebtLedger).where(
            DebtLedger.id == debt_id,
            DebtLedger.customer_id == customer_id,
        )
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Debt entry not found")
    return entry


@app.get("/api/customers/{customer_id}/balance", response_model=CustomerBalanceResponse)
async def get_customer_balance(customer_id: int, db: DbSession) -> CustomerBalanceResponse:
    """Per-customer debt summary addressed by customer id."""
    await _get_customer_or_404(db, customer_id)
    charges, payments = await _debt_totals_for(db, customer_id)
    return CustomerBalanceResponse(
        total_debt_cents=charges - payments,
        total_charges_cents=charges,
        total_payments_cents=payments,
    )


@app.post("/api/customers/{customer_id}/debt", response_model=DebtLedgerResponse)
async def add_customer_debt(
    customer_id: int, data: CustomerDebtCreate, db: DbSession
) -> DebtLedgerResponse:
    """Record a manual charge or payment against an existing customer."""
    customer = await _get_customer_or_404(db, customer_id)

    register_id = None
    if data.type == "payment":
        # Mirror /api/debt/payment: payments belong to the open register if any.
        register = await db.scalar(select(CashRegister).where(CashRegister.status == "open"))
        register_id = register.id if register is not None else None

    entry = DebtLedger(
        customer_id=customer.id,
        register_id=register_id,
        amount_cents=data.amount_cents,
        description=data.description,
        type=data.type,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return _debt_entry_response(entry, customer)


@app.put("/api/customers/{customer_id}/debt/{debt_id}", response_model=DebtLedgerResponse)
async def update_customer_debt(
    customer_id: int, debt_id: int, data: CustomerDebtUpdate, db: DbSession
) -> DebtLedgerResponse:
    """Edit the amount, description and/or type of one debt entry."""
    customer = await _get_customer_or_404(db, customer_id)
    entry = await _get_debt_entry_or_404(db, customer_id, debt_id)

    if data.amount_cents is not None:
        entry.amount_cents = data.amount_cents
    if data.description is not None:
        entry.description = data.description
    if data.type is not None:
        entry.type = data.type
    await db.commit()
    await db.refresh(entry)
    return _debt_entry_response(entry, customer)


@app.delete("/api/customers/{customer_id}/debt/{debt_id}", status_code=204, response_class=Response)
async def delete_customer_debt(customer_id: int, debt_id: int, db: DbSession) -> Response:
    """Remove a single debt entry from a customer's ledger."""
    await _get_customer_or_404(db, customer_id)
    entry = await _get_debt_entry_or_404(db, customer_id, debt_id)
    await db.delete(entry)
    await db.commit()
    return Response(status_code=204)


# --- Operator balance endpoints ---


@app.get("/api/operators/balance", response_model=list[OperatorBalanceResponse])
async def list_operator_balances(db: DbSession) -> list[OperatorBalanceResponse]:
    """Return the prepaid credit held with every operator (0 when no row exists)."""
    rows = await db.execute(
        select(
            Operator.code,
            Operator.name,
            func.coalesce(OperatorBalance.balance_cents, 0).label("balance_cents"),
        )
        .outerjoin(OperatorBalance, OperatorBalance.operator_id == Operator.id)
        .order_by(Operator.code)
    )
    return [
        OperatorBalanceResponse(
            operator_code=row.code,
            operator_name=row.name,
            balance_cents=int(row.balance_cents or 0),
        )
        for row in rows.all()
    ]


@app.post("/api/operators/topup", response_model=OperatorTopupResponse)
async def topup_operator_balance(
    data: OperatorTopupCreate, db: DbSession
) -> OperatorTopupResponse:
    """Add credit to an operator balance and record the top-up."""
    operator = await db.scalar(select(Operator).where(Operator.code == data.operator_code))
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator not found")

    balance = await db.scalar(
        select(OperatorBalance).where(OperatorBalance.operator_id == operator.id)
    )
    if balance is None:
        balance = OperatorBalance(operator_id=operator.id, balance_cents=0)
        db.add(balance)
        await db.flush()

    balance.balance_cents += data.amount_cents
    reference = f"TOPUP-{uuid4().hex}"
    db.add(
        OperatorTopup(
            operator_id=operator.id,
            amount_cents=data.amount_cents,
            reference=reference,
        )
    )
    await db.commit()
    await db.refresh(balance)
    return OperatorTopupResponse(
        operator_code=operator.code,
        amount_cents=data.amount_cents,
        new_balance_cents=balance.balance_cents,
        reference=reference,
    )


@app.post("/api/transactions", response_model=TransactionResponse)
async def create_transaction(data: TransactionCreate, db: DbSession) -> Transaction:
    operator = await db.scalar(select(Operator).where(Operator.code == data.operator_code))
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator not found")
    service = await db.scalar(
        select(Service).where(
            Service.operator_id == operator.id, Service.code == data.service_code
        )
    )
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found for operator")
    if not operator.is_active or not service.is_active:
        raise HTTPException(status_code=400, detail="Operator or service is inactive")

    try:
        ussd_string = await generate_ussd(
            db, data.operator_code, data.service_code, data.customer_phone, data.amount_cents
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # The shop must hold enough prepaid credit with the operator; rejecting
    # before anything is written keeps the request atomic.
    balance = await db.scalar(
        select(OperatorBalance).where(OperatorBalance.operator_id == operator.id)
    )
    if balance is None or balance.balance_cents < data.amount_cents:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Insufficient operator balance")

    try:
        commission = await get_commission_for_transaction(db, data.amount_cents)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid stored commission rate") from exc
    commission_cents = int(commission["commission_cents"])

    customer = await db.scalar(select(Customer).where(Customer.phone == data.customer_phone))
    is_new_customer = customer is None
    if customer is None:
        # The transaction request has no customer name; use the phone as a placeholder.
        customer = Customer(phone=data.customer_phone, name=data.customer_phone)
        db.add(customer)

    transaction = Transaction(
        customer=customer,
        service=service,
        amount_cents=data.amount_cents,
        status="pending",
        ussd_string=ussd_string,
        reference=f"TXN-{uuid4().hex}",
    )
    db.add(transaction)
    balance.balance_cents -= data.amount_cents
    try:
        await db.flush()  # assigns transaction.id for the profit ledger row
        db.add(
            ProfitLedger(
                transaction_id=transaction.id,
                operator_code=operator.code,
                amount_cents=data.amount_cents,
                commission_cents=commission_cents,
                profit_cents=data.amount_cents - commission_cents,
            )
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Concurrent requests can attempt to create the same new customer.
        existing = await db.scalar(select(Customer).where(Customer.phone == data.customer_phone))
        if is_new_customer and existing is not None:
            raise HTTPException(status_code=409, detail="Customer was created concurrently; retry")
        raise
    await db.refresh(transaction)
    return transaction


def _transaction_list_statement():
    """Base select of transactions joined with customer/operator/service labels."""
    return (
        select(
            Transaction.id,
            Transaction.reference,
            Transaction.ussd_string,
            Transaction.status,
            Transaction.amount_cents,
            Transaction.created_at,
            Customer.phone.label("customer_phone"),
            Customer.name.label("customer_name"),
            Operator.code.label("operator_code"),
            Operator.name.label("operator_name"),
            Service.code.label("service_code"),
            Service.name.label("service_name"),
        )
        .join(Customer, Transaction.customer_id == Customer.id)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
    )


@app.get("/api/transactions", response_model=list[TransactionListItem])
async def list_transactions(
    db: DbSession,
    operator_code: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    service_code: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    status: Annotated[str | None, Query(pattern="^(pending|completed|failed)$")] = None,
    customer_phone: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
) -> list[TransactionListItem]:
    """List transactions newest-first, optionally filtered by operator code,
    service code, status, or a case-insensitive customer phone fragment.
    Filters that match nothing return an empty list."""
    statement = _transaction_list_statement()
    if operator_code is not None:
        statement = statement.where(Operator.code == operator_code)
    if service_code is not None:
        statement = statement.where(Service.code == service_code)
    if status is not None:
        statement = statement.where(Transaction.status == status)
    if customer_phone is not None:
        statement = statement.where(Customer.phone.ilike(f"%{customer_phone}%"))
    rows = await db.execute(statement.order_by(Transaction.created_at.desc(), Transaction.id.desc()))
    return [TransactionListItem(**row._mapping) for row in rows.all()]


# --- Debt Ledger Endpoints ---


@app.post("/api/debt/charge", response_model=DebtLedgerResponse)
async def create_debt_charge(data: DebtLedgerCreate, db: DbSession) -> DebtLedger:
    """Create a debt charge for a customer (lookup by phone, create if not exists)."""
    try:
        phone = validate_syrian_phone(data.customer_phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    customer = await db.scalar(select(Customer).where(Customer.phone == phone))
    if customer is None:
        # Create customer with phone as name if not exists
        customer = Customer(phone=phone, name=phone)
        db.add(customer)
        await db.flush()  # Get the ID

    debt_entry = DebtLedger(
        customer_id=customer.id,
        amount_cents=data.amount_cents,
        description=data.description,
        type=data.type,
    )
    db.add(debt_entry)
    await db.commit()
    await db.refresh(debt_entry)

    # Build response with customer_phone
    return DebtLedgerResponse(
        id=debt_entry.id,
        customer_phone=customer.phone,
        amount_cents=debt_entry.amount_cents,
        description=debt_entry.description,
        type=debt_entry.type,
        created_at=debt_entry.created_at,
    )


@app.get("/api/debt/ledger", response_model=list[DebtLedgerResponse])
async def get_debt_ledger(
    db: DbSession,
    customer_phone: Annotated[str, Query()],
) -> list[DebtLedgerResponse]:
    """List debt ledger entries for a customer."""
    try:
        phone = validate_syrian_phone(customer_phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    customer = await db.scalar(select(Customer).where(Customer.phone == phone))
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    result = await db.scalars(
        select(DebtLedger).where(DebtLedger.customer_id == customer.id).order_by(DebtLedger.created_at.desc())
    )
    entries = result.all()

    return [
        DebtLedgerResponse(
            id=entry.id,
            customer_phone=customer.phone,
            amount_cents=entry.amount_cents,
            description=entry.description,
            type=entry.type,
            created_at=entry.created_at,
        )
        for entry in entries
    ]


@app.get("/api/debt/summary")
async def get_debt_summary(
    db: DbSession,
    customer_phone: Annotated[str, Query()],
) -> dict[str, int]:
    """Return total debt (sum of charges minus payments) for a customer."""
    try:
        phone = validate_syrian_phone(customer_phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    customer = await db.scalar(select(Customer).where(Customer.phone == phone))
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    result = await db.scalars(
        select(DebtLedger).where(DebtLedger.customer_id == customer.id)
    )
    entries = result.all()

    total_charges = sum(e.amount_cents for e in entries if e.type == "charge")
    total_payments = sum(e.amount_cents for e in entries if e.type == "payment")
    total_debt = total_charges - total_payments

    return {
        "customer_phone": customer.phone,
        "total_charges_cents": total_charges,
        "total_payments_cents": total_payments,
        "total_debt_cents": total_debt,
    }


# --- Debt Payment Endpoint ---


@app.post("/api/debt/payment", response_model=DebtLedgerResponse)
async def record_debt_payment(
    data: DebtPaymentCreate,
    db: DbSession,
) -> DebtLedger:
    """Record a debt payment for a customer."""
    try:
        phone = validate_syrian_phone(data.customer_phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    customer = await db.scalar(select(Customer).where(Customer.phone == phone))
    if customer is None:
        # Create customer with phone as name if not exists
        customer = Customer(phone=phone, name=phone)
        db.add(customer)
        await db.flush()

    # Find open register if any
    register = await db.scalar(
        select(CashRegister).where(CashRegister.status == "open")
    )

    debt_entry = DebtLedger(
        customer_id=customer.id,
        register_id=register.id if register else None,
        amount_cents=data.amount_cents,
        description=data.description,
        type="payment",
    )
    db.add(debt_entry)
    await db.commit()
    await db.refresh(debt_entry)

    # Build response with customer_phone
    return DebtLedgerResponse(
        id=debt_entry.id,
        customer_phone=customer.phone,
        amount_cents=debt_entry.amount_cents,
        description=debt_entry.description,
        type=debt_entry.type,
        created_at=debt_entry.created_at,
    )


# --- Currency Denomination Endpoints ---


@app.get("/api/currency/denomination", response_model=CurrencyDenominationResponse)
async def get_active_denomination(db: DbSession) -> CurrencyDenomination:
    """Return the active currency denomination."""
    denom = await db.scalar(
        select(CurrencyDenomination).where(CurrencyDenomination.is_active == True)
    )
    if denom is None:
        raise HTTPException(status_code=404, detail="No active currency denomination found")
    return denom


@app.put("/api/currency/denomination", response_model=CurrencyDenominationResponse)
async def set_active_denomination(data: SetDenomination, db: DbSession) -> CurrencyDenomination:
    """Set active denomination (deactivate all others)."""
    denom = await db.scalar(
        select(CurrencyDenomination).where(CurrencyDenomination.scale_name == data.scale_name)
    )
    if denom is None:
        raise HTTPException(status_code=404, detail="Currency denomination not found")

    # Deactivate all others
    await db.execute(
        select(CurrencyDenomination).where(CurrencyDenomination.is_active == True)
    )
    all_denoms = await db.scalars(select(CurrencyDenomination).where(CurrencyDenomination.is_active == True))
    for d in all_denoms:
        d.is_active = False
        await db.flush()

    denom.is_active = True
    await db.commit()
    await db.refresh(denom)

    return denom


def format_amount(amount_cents: int, denomination: CurrencyDenomination) -> str:
    """Format amount in Syrian Pounds with ل.س symbol."""
    multiplier = denomination.multiplier if denomination else 1
    value = amount_cents / multiplier
    return f"{value:,.2f} ل.س"


# --- Cash Register Endpoints ---
# --- Cash Register Endpoints ---


@app.post("/api/register/open", response_model=CashRegisterResponse)
async def open_register(
    data: CashRegisterCreate,
    db: DbSession,
) -> CashRegister:
    """Open a new register. Fail if one is already open."""
    # Check if a register is already open
    existing = await db.scalar(
        select(CashRegister).where(CashRegister.status == "open")
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="A register is already open")

    register = CashRegister(
        opening_balance_cents=data.opening_balance_cents,
        status="open",
        opened_at=datetime.now(timezone.utc),
        closed_at=None,
    )
    db.add(register)
    await db.commit()
    await db.refresh(register)

    return register


@app.post("/api/register/close", response_model=CashRegisterResponse)
async def close_register(
    db: DbSession,
    register_id: int,
) -> CashRegister:
    """Close the current open register, compute closing balance."""
    register = await db.get(CashRegister, register_id)
    if register is None:
        raise HTTPException(status_code=404, detail="Register not found")

    if register.status != "open":
        raise HTTPException(status_code=400, detail="Can only close an open register")

    # Compute cash collected: sum of completed transaction amount_cents since register opened
    transactions_result = await db.scalars(
        select(Transaction)
        .where(
            Transaction.created_at >= register.opened_at,
            Transaction.status == "completed",
        )
    )
    transactions = transactions_result.all()
    cash_collected = sum(t.amount_cents for t in transactions)

    # Compute debt collected: sum of DebtLedger payments (type='payment') since register opened
    debt_result = await db.scalars(
        select(DebtLedger)
        .where(
            DebtLedger.created_at >= register.opened_at,
            DebtLedger.type == "payment",
        )
    )
    debt_entries = debt_result.all()
    debt_collected = sum(d.amount_cents for d in debt_entries)

    register.closing_balance_cents = register.opening_balance_cents + cash_collected + debt_collected
    register.status = "closed"
    register.closed_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(register)

    return register


@app.get("/api/register/summary", response_model=RegisterSummary)
async def get_register_summary(
    db: DbSession,
) -> RegisterSummary:
    """Return summary for current/open register, or last closed one."""
    # Try to find open register first
    register = await db.scalar(
        select(CashRegister).where(CashRegister.status == "open")
    )

    if register is None:
        # Fall back to most recent closed register
        register = await db.scalar(
            select(CashRegister).where(CashRegister.status == "closed").order_by(CashRegister.closed_at.desc())
        )

    if register is None:
        raise HTTPException(status_code=404, detail="No register found")

    # Compute cash collected: sum of completed transaction amount_cents since register opened
    transactions_result = await db.scalars(
        select(Transaction)
        .where(
            Transaction.created_at >= register.opened_at,
            Transaction.status == "completed",
        )
    )
    transactions = transactions_result.all()
    cash_collected = sum(t.amount_cents for t in transactions)

    # Compute debt collected: sum of DebtLedger payments (type='payment') since register opened
    debt_result = await db.scalars(
        select(DebtLedger)
        .where(
            DebtLedger.created_at >= register.opened_at,
            DebtLedger.type == "payment",
        )
    )
    debt_entries = debt_result.all()
    debt_collected = sum(d.amount_cents for d in debt_entries)

    closing_balance = register.opening_balance_cents + cash_collected + debt_collected

    return RegisterSummary(
        opening_balance_cents=register.opening_balance_cents,
        cash_collected_cents=cash_collected,
        debt_collected_cents=debt_collected,
        closing_balance_cents=closing_balance if register.status == "closed" else None,
        transaction_count=len(transactions),
        debt_payment_count=len(debt_entries),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
