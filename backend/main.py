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
    from .database import Customer, Operator, Service, Transaction, DebtLedger, get_db, get_setting, set_setting
    from .seed import generate_ussd
    from .validation import validate_syrian_phone
else:
    from commission import get_commission_for_transaction
    from database import Customer, Operator, Service, Transaction, DebtLedger, get_db, get_setting, set_setting
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
    type: str = Field(default="charge", pattern="^charge|payment$")

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


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    return HTMLResponse(
        "<h1>Uniter — Mobile Shop Charging</h1>"
        "<p>Bilingual MTN/Syriatel prepaid & postpaid management.</p>"
    )


@app.get("/pos", response_class=HTMLResponse)
def pos_page(request: Request, lang: str = "en") -> HTMLResponse:
    language = lang if lang in {"en", "ar"} else "en"
    return templates.TemplateResponse(request=request, name="pos.html", context={"lang": language})


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
