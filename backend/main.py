from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
import os
import re

from backend.database import SessionLocal, init_db, OperatorModel, ServiceModel, TransactionModel, CustomerModel, LedgerEntryModel, engine

app = FastAPI(title="Uniter Shop Charging", version="1.0.0")

init_db(engine)

os.makedirs("/home/jabboud/projects/Uniter/backend/static", exist_ok=True)
os.makedirs("/home/jabboud/projects/Uniter/backend/templates", exist_ok=True)

app.mount("/static", StaticFiles(directory="/home/jabboud/projects/Uniter/backend/static"), name="static")
templates = Jinja2Templates(directory="/home/jabboud/projects/Uniter/backend/templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class TransactionCreate(BaseModel):
    operator_name: str
    service_type: str
    phone_number: str
    amount: float
    note: str | None = None
    customer_id: int | None = None
    is_debt: bool = False

    @field_validator('phone_number')
    @classmethod
    def validate_syrian_phone(cls, v: str) -> str:
        cleaned = re.sub(r'\D', '', v)
        if not re.match(r'^09\d{8}$', cleaned):
            raise ValueError('رقم الهاتف غير صالح. يجب أن يبدأ ب 09 ويتكون من 10 أرقام (Invalid Syrian phone number)')
        return cleaned

class CustomerCreate(BaseModel):
    name: str
    phone: str

class DebtSettle(BaseModel):
    amount: float

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    operators = db.query(OperatorModel).all()
    services = db.query(ServiceModel).all()
    transactions = db.query(TransactionModel).order_by(TransactionModel.id.desc()).limit(15).all()
    customers = db.query(CustomerModel).all()
    
    # Calculate register summary
    total_sales = sum(t.amount for t in db.query(TransactionModel).all())
    total_commission = sum(t.commission for t in db.query(TransactionModel).all())
    total_debt = sum(c.debt_balance for c in customers)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "operators": operators,
        "services": services,
        "transactions": transactions,
        "customers": customers,
        "total_sales": total_sales,
        "total_commission": total_commission,
        "total_debt": total_debt
    })

@app.get("/api/health")
def health_check(db: Session = Depends(get_db)):
    ops = [o.name for o in db.query(OperatorModel).all()]
    return {"status": "ok", "project": "Uniter", "operators": ops}

@app.get("/api/operators")
def api_get_operators(db: Session = Depends(get_db)):
    return db.query(OperatorModel).all()

@app.get("/api/services")
def api_get_services(db: Session = Depends(get_db)):
    return db.query(ServiceModel).all()

@app.get("/api/customers")
def api_get_customers(db: Session = Depends(get_db)):
    return db.query(CustomerModel).all()

@app.post("/api/customers")
def api_create_customer(c: CustomerCreate, db: Session = Depends(get_db)):
    existing = db.query(CustomerModel).filter(CustomerModel.name == c.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Customer already exists")
    cust = CustomerModel(name=c.name, phone=c.phone, debt_balance=0.0)
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return cust

@app.post("/api/customers/{customer_id}/settle")
def api_settle_debt(customer_id: int, payload: DebtSettle, db: Session = Depends(get_db)):
    cust = db.query(CustomerModel).filter(CustomerModel.id == customer_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    cust.debt_balance = max(0.0, cust.debt_balance - payload.amount)
    
    entry = LedgerEntryModel(
        customer_id=cust.id,
        entry_type="debt_settled",
        amount=payload.amount,
        description=f"تسديد دين للزبون {cust.name}"
    )
    db.add(entry)
    db.commit()
    db.refresh(cust)
    return cust

@app.post("/api/transactions")
def api_create_transaction(tx: TransactionCreate, db: Session = Depends(get_db)):
    service = db.query(ServiceModel).join(OperatorModel).filter(
        OperatorModel.name == tx.operator_name,
        ServiceModel.service_type == tx.service_type
    ).first()
    
    ussd = "*150*#"
    if service:
        ussd = service.ussd_template.format(phone=tx.phone_number, amount=int(tx.amount))
    
    commission = round(tx.amount * 0.01, 2)
    
    db_tx = TransactionModel(
        operator_name=tx.operator_name,
        service_type=tx.service_type,
        phone_number=tx.phone_number,
        amount=tx.amount,
        ussd_string=ussd,
        commission=commission,
        note=tx.note,
        status="completed"
    )
    db.add(db_tx)
    
    # Handle debt if assigned to customer
    if tx.customer_id and tx.is_debt:
        cust = db.query(CustomerModel).filter(CustomerModel.id == tx.customer_id).first()
        if cust:
            cust.debt_balance += tx.amount
            ledger = LedgerEntryModel(
                customer_id=cust.id,
                entry_type="debt_added",
                amount=tx.amount,
                description=f"شحن دين برقم {tx.phone_number} ({tx.operator_name})"
            )
            db.add(ledger)
            
    db.commit()
    db.refresh(db_tx)
    return db_tx

@app.get("/api/register/summary")
def api_register_summary(db: Session = Depends(get_db)):
    transactions = db.query(TransactionModel).all()
    customers = db.query(CustomerModel).all()
    return {
        "total_sales": sum(t.amount for t in transactions),
        "total_commission": sum(t.commission for t in transactions),
        "total_debt": sum(c.debt_balance for c in customers),
        "transaction_count": len(transactions)
    }
