from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Dict
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, func, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.sql.selectable import Subquery
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

if __package__:
    from .commission import get_commission_for_transaction
    from .database import Customer, Operator, Service, Transaction, DebtLedger, OperatorBalance, OperatorTopup, CashRegister, CurrencyDenomination, get_db, get_setting, set_setting
    from .seed import generate_ussd
    from .validation import validate_syrian_phone
else:
    from commission import get_commission_for_transaction
    from database import Customer, Operator, Service, Transaction, DebtLedger, OperatorBalance, OperatorTopup, CashRegister, CurrencyDenomination, get_db, get_setting, set_setting
    from seed import generate_ussd
    from validation import validate_syrian_phone

app = FastAPI(title="Uniter")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

Code = Annotated[str, Field(min_length=1, max_length=50)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


class CustomerCreate(BaseModel):
    phone: str
    name: str = Field(min_length=1, max_length=255)

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


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    ussd_string: str
    status: str
    amount_cents: int
    created_at: datetime


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
async def pos_page(request: Request, lang: str = "en") -> HTMLResponse:
    async with get_db() as db:
        language = lang if lang in {"en", "ar"} else "en"
        denom = await db.scalar(select(CurrencyDenomination).where(CurrencyDenomination.is_active == True))
        return templates.TemplateResponse(request=request, name="pos.html", context={"lang": language, "currency_denomination": denom})

@app.get("/settlement", response_class=HTMLResponse)
async def settlement_page(request: Request, lang: str = "en") -> HTMLResponse:
    async with get_db() as db:
        language = lang if lang in {"en", "ar"} else "en"
        denom = await db.scalar(select(CurrencyDenomination).where(CurrencyDenomination.is_active == True))
        return templates.TemplateResponse(request=request, name="settlement.html", context={"lang": language, "currency_denomination": denom})


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


@app.post("/api/customers", response_model=CustomerResponse)
async def create_customer(data: CustomerCreate, db: DbSession) -> Customer:
    customer = Customer(phone=data.phone, name=data.name)
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


@app.get("/api/operators/balance", response_model=OperatorBalanceResponse)
async def get_operator_balance(
    db: DbSession,
    operator_code: Annotated[str, Query(min_length=1, max_length=50)],
) -> OperatorBalanceResponse:
    """Return the prepaid credit held with one operator."""
    operator = await db.scalar(select(Operator).where(Operator.code == operator_code))
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator not found")
    balance = await db.scalar(
        select(OperatorBalance.balance_cents).where(OperatorBalance.operator_id == operator.id)
    )
    return OperatorBalanceResponse(
        operator_code=operator.code,
        operator_name=operator.name,
        balance_cents=int(balance or 0),
    )


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
    try:
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
