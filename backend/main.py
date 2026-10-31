from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

if __package__:
    from .database import Customer, Operator, Service, Transaction, get_db
    from .seed import generate_ussd
    from .validation import validate_syrian_phone
else:
    from database import Customer, Operator, Service, Transaction, get_db
    from seed import generate_ussd
    from validation import validate_syrian_phone

app = FastAPI(title="Uniter")

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
    amount_cents: int = Field(ge=0, le=9223372036854775807, strict=True)

    @field_validator("customer_phone")
    @classmethod
    def validate_customer_phone(cls, phone: str) -> str:
        return validate_syrian_phone(phone)


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    ussd_string: str
    status: str
    amount_cents: int
    created_at: datetime


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    return HTMLResponse(
        "<h1>Uniter — Mobile Shop Charging</h1>"
        "<p>Bilingual MTN/Syriatel prepaid & postpaid management.</p>"
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
