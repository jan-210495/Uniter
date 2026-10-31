from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import datetime

SQLALCHEMY_DATABASE_URL = "sqlite:///./uniter.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class OperatorModel(Base):
    __tablename__ = "operators"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    display_name_ar = Column(String)
    display_name_en = Column(String)
    color_class = Column(String)

class ServiceModel(Base):
    __tablename__ = "services"
    
    id = Column(Integer, primary_key=True, index=True)
    operator_id = Column(Integer, ForeignKey("operators.id"))
    service_type = Column(String)
    name_ar = Column(String)
    name_en = Column(String)
    ussd_template = Column(String)

class CustomerModel(Base):
    __tablename__ = "customers"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    phone = Column(String)
    debt_balance = Column(Float, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))

class LedgerEntryModel(Base):
    __tablename__ = "ledger_entries"
    
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    entry_type = Column(String) # debt_added, debt_settled, cash_sale
    amount = Column(Float)
    description = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))

class TransactionModel(Base):
    __tablename__ = "transactions"
    
    id = Column(Integer, primary_key=True, index=True)
    operator_name = Column(String)
    service_type = Column(String)
    phone_number = Column(String)
    amount = Column(Float)
    ussd_string = Column(String)
    commission = Column(Float, default=0.0)
    note = Column(String, nullable=True)
    status = Column(String, default="completed")
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))

def init_db(db_engine=engine):
    Base.metadata.create_all(bind=db_engine)
    SessionMaker = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    db = SessionMaker()
    if not db.query(OperatorModel).first():
        mtn = OperatorModel(name="MTN", display_name_ar="إم تي إن", display_name_en="MTN", color_class="blue")
        syriatel = OperatorModel(name="Syriatel", display_name_ar="سيريتل", display_name_en="Syriatel", color_class="red")
        db.add_all([mtn, syriatel])
        db.commit()
        
        services = [
            ServiceModel(operator_id=mtn.id, service_type="prepaid_units", name_ar="شحن وحدات (دفع مسبق)", name_en="Pre-Paid Units Recharge", ussd_template="*150*{phone}*{amount}#"),
            ServiceModel(operator_id=mtn.id, service_type="postpaid_bill", name_ar="دفع فاتورة (لاحق الدفع)", name_en="Post-Paid Bill Payment", ussd_template="*151*{phone}*{amount}#"),
            ServiceModel(operator_id=syriatel.id, service_type="prepaid_units", name_ar="شحن رصيد (دفع مسبق)", name_en="Pre-Paid Balance Recharge", ussd_template="*250*{phone}*{amount}#"),
            ServiceModel(operator_id=syriatel.id, service_type="postpaid_bill", name_ar="دفع فاتورة (لاحق الدفع)", name_en="Post-Paid Bill Payment", ussd_template="*251*{phone}*{amount}#"),
        ]
        db.add_all(services)
        db.commit()
    db.close()
