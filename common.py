import os
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, DateTime, Text, Boolean, UniqueConstraint, select
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

class HistoricalDelivery(Base):
    """Durable, per-customer queue for already-closed recommendations sent after activation."""
    __tablename__='historical_deliveries'
    __table_args__=(UniqueConstraint('telegram_id','signal_id',name='uq_historical_customer_signal'),)
    id=Column(Integer,primary_key=True,autoincrement=True)
    telegram_id=Column(BigInteger,nullable=False,index=True)
    signal_id=Column(String(120),nullable=False)
    payload=Column(Text,nullable=False)
    status=Column(String(20),default='queued',nullable=False)
    sent_at=Column(DateTime(timezone=True))

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


CLOSED_KINDS=('target2','stop','eod')

def queue_recent_closed(db, telegram_id, limit=5):
    """Queue most recent distinct *closed* recommendations, never as live alerts.

    Historical recommendations are delivered once per customer/signal even after renewal.
    If there is no closed event stored yet, return 0 (no fabricated history).
    """
    endings=db.scalars(select(Event).where(Event.kind.in_(CLOSED_KINDS))
                       .order_by(Event.id.desc())).all()
    added=0
    seen=set()
    for end in endings:
        sid=end.signal_id
        if sid in seen: continue
        seen.add(sid)
        exists=db.scalar(select(HistoricalDelivery.id).where(
            HistoricalDelivery.telegram_id==telegram_id,
            HistoricalDelivery.signal_id==sid))
        if exists:continue
        opening=db.scalar(select(Event).where(Event.signal_id==sid,Event.kind=='open')
                          .order_by(Event.id.asc()).limit(1))
        parts=['📚 توصية مغلقة سابقة — للعرض فقط',
               '⚠️ ليست إشارة دخول جديدة',
               f'السهم: {end.symbol}',
               f'معرّف التوصية: {sid}']
        if opening:parts.extend(['', '📈 إشارة الدخول الأصلية:', opening.payload])
        parts.extend(['', '🏁 آخر تحديث لإغلاق التوصية:', end.payload])
        content='\n'.join(parts)
        if len(content)>3900:
            # Telegram max 4096; retain both the original signal and final status.
            header='\n'.join(parts[:4])
            entry=(opening.payload[:1400] if opening else 'غير محفوظة')
            final=end.payload[:1400]
            content=f'{header}\n\n📈 إشارة الدخول الأصلية:\n{entry}\n\n🏁 آخر تحديث للإغلاق:\n{final}'
        db.add(HistoricalDelivery(telegram_id=telegram_id,signal_id=sid,payload=content))
        added+=1
        if added>=limit:break
    return added
