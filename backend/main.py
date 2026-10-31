from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Dict
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

if __package__:
    from .commission import get_commission_for_transaction
    from .database import Customer, Operator, Service, Transaction, DebtLedger, CashRegister, CurrencyDenomination, get_db, get_setting, set_setting
    from .seed import generate_ussd
    from .validation import validate_syrian_phone
else:
    from commission import get_commission_for_transaction
    from database import Customer, Operator, Service, Transaction, DebtLedger, CashRegister, CurrencyDenomination, get_db, get_setting, set_setting
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
