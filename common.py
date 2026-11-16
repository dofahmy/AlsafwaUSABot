import os
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, DateTime, Text, Boolean, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./rajih_customers.db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://','postgresql://',1)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite:') else {})
Session = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

def utcnow(): return datetime.now(timezone.utc)
class Customer(Base):
    __tablename__='customers'
    telegram_id=Column(BigInteger,primary_key=True)
    username=Column(String(128))
    created_at=Column(DateTime(timezone=True),default=utcnow)
    access_until=Column(DateTime(timezone=True))
    trial_used=Column(Boolean,default=False,nullable=False)
    status=Column(String(24),default='inactive')

class Request(Base):
    __tablename__='activation_requests'
    id=Column(Integer,primary_key=True,autoincrement=True)
    telegram_id=Column(BigInteger,nullable=False,index=True)
    kind=Column(String(20),nullable=False) # trial/subscription
    status=Column(String(20),default='pending',nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow)
    decided_at=Column(DateTime(timezone=True))

class Event(Base):
    __tablename__='signal_events'
    id=Column(Integer,primary_key=True,autoincrement=True)
    event_key=Column(String(200),unique=True,nullable=False)
    signal_id=Column(String(120),nullable=False,index=True)
    symbol=Column(String(32),nullable=False)
    kind=Column(String(32),nullable=False)
    payload=Column(Text,nullable=False)
    happened_at=Column(DateTime(timezone=True),default=utcnow)

class Delivery(Base):
    __tablename__='signal_deliveries'
    __table_args__=(UniqueConstraint('event_id','telegram_id',name='uq_delivered_event_customer'),)
    id=Column(Integer,primary_key=True,autoincrement=True)
    event_id=Column(Integer,nullable=False,index=True)
    telegram_id=Column(BigInteger,nullable=False)
    status=Column(String(20),default='queued')
    sent_at=Column(DateTime(timezone=True))
    error=Column(Text)

class Payment(Base):
    __tablename__='external_payments'
    id=Column(Integer,primary_key=True)
    provider=Column(String(40),nullable=False)
    provider_payment_id=Column(String(180),unique=True,nullable=False)
    telegram_id=Column(BigInteger,nullable=False)
    period_days=Column(Integer,nullable=False)
    confirmed_at=Column(DateTime(timezone=True),default=utcnow)

Base.metadata.create_all(engine)

def active(customer):
    if not customer or not customer.access_until: return False
    dt=customer.access_until
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    return dt>utcnow()

def grant(db, customer, days, kind):
    base=customer.access_until if active(customer) else utcnow()
    if base.tzinfo is None: base=base.replace(tzinfo=timezone.utc)
    customer.access_until=base+timedelta(days=days)
    customer.status='active'
    if kind=='trial': customer.trial_used=True
    db.add(customer)
    return customer.access_until
