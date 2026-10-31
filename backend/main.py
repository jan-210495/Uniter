from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
import os
import re

from backend.database import SessionLocal, init_db, OperatorModel, ServiceModel, TransactionModel, engine

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

    @field_validator('phone_number')
    @classmethod
    def validate_syrian_phone(cls, v: str) -> str:
        # Must start with 09 and be 10 digits
        cleaned = re.sub(r'\D', '', v)
        if not re.match(r'^09\d{8}$', cleaned):
            raise ValueError('رقم الهاتف غير صالح. يجب أن يبدأ ب 09 ويتكون من 10 أرقام (Invalid Syrian phone number)')
        return cleaned

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    operators = db.query(OperatorModel).all()
    services = db.query(ServiceModel).all()
    transactions = db.query(TransactionModel).order_by(TransactionModel.id.desc()).limit(15).all()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "operators": operators,
        "services": services,
        "transactions": transactions
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

@app.post("/api/transactions")
def api_create_transaction(tx: TransactionCreate, db: Session = Depends(get_db)):
    service = db.query(ServiceModel).join(OperatorModel).filter(
        OperatorModel.name == tx.operator_name,
        ServiceModel.service_type == tx.service_type
    ).first()
    
    ussd = "*150*#"
    if service:
        ussd = service.ussd_template.format(phone=tx.phone_number, amount=int(tx.amount))
    
    # Calculate nominal shop commission (e.g. 1%)
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
    db.commit()
    db.refresh(db_tx)
    return db_tx

@app.get("/api/transactions")
def api_get_transactions(db: Session = Depends(get_db)):
    return db.query(TransactionModel).order_by(TransactionModel.id.desc()).limit(50).all()
