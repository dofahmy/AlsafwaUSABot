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

class PremarketPost(Base):
    """صورة «نظرة قبل الافتتاح» وتعليقها (آخر كام يوم بس)."""
    __tablename__='premarket_posts'
    id=Column(Integer,primary_key=True,autoincrement=True)
    day=Column(String(10),nullable=False,index=True)
    caption=Column(Text,nullable=False)
    image_b64=Column(Text,nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow)

class Credit(Base):
    """رصيد العميل بعدد التوصيات (بدل المدة)."""
    __tablename__='credits'
    telegram_id=Column(BigInteger,primary_key=True)
    mode=Column(String(12),default='none',nullable=False)    # none / trial / paid
    balance=Column(Integer,default=0,nullable=False)          # توصيات مدفوعة متبقية
    trial_wins=Column(Integer,default=0,nullable=False)       # توصيات ناجحة خلال التجربة
    updated_at=Column(DateTime(timezone=True),default=utcnow)

class CreditLedger(Base):
    """كل حركة على الرصيد (شراء، خصم، تعويض، نجاح في التجربة) — حركة واحدة بس لكل توصية ونوع."""
    __tablename__='credit_ledger'
    __table_args__=(UniqueConstraint('telegram_id','ref','kind',name='uq_credit_ref_kind'),)
    id=Column(Integer,primary_key=True,autoincrement=True)
    telegram_id=Column(BigInteger,nullable=False,index=True)
    ref=Column(String(180),nullable=False)          # signal_id أو رقم الدفعة
    kind=Column(String(24),nullable=False)          # charge / refund_cancel / refund_stop / purchase / trial_win / admin
    delta=Column(Integer,default=0,nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow)

Base.metadata.create_all(engine)

TRIAL_WINS=int(os.getenv('TRIAL_WINS','5'))        # التجربة المجانية = عدد التوصيات الناجحة
PACKAGE_SIZE=int(os.getenv('PACKAGE_SIZE','10'))   # كل باقة = ١٠ توصيات


def credit(db, tid):
    c=db.get(Credit,tid)
    if not c:
        c=Credit(telegram_id=tid,mode='none',balance=0,trial_wins=0);db.add(c);db.flush()
    return c


def eligible(db, tid):
    """العميل يستلم توصيات جديدة؟ رصيد مدفوع > 0، أو في التجربة ولسه ما وصلش لـ TRIAL_WINS توصية ناجحة."""
    c=db.get(Credit,tid)
    if not c:return False
    return c.balance>0 or (c.mode=='trial' and c.trial_wins<TRIAL_WINS)


def active(customer):
    if not customer:return False
    with Session() as s:
        return eligible(s,customer.telegram_id)


def has_purchased(db, tid):
    return bool(db.scalar(select(CreditLedger.id).where(CreditLedger.telegram_id==tid,CreditLedger.kind.in_(('purchase','admin')))))


def _ledger(db, tid, ref, kind, delta):
    """حركة على الرصيد مرة واحدة بس لنفس (العميل، المرجع، النوع). بترجّع False لو اتعملت قبل كده."""
    if db.scalar(select(CreditLedger.id).where(CreditLedger.telegram_id==tid,CreditLedger.ref==ref,CreditLedger.kind==kind)):
        return False
    db.add(CreditLedger(telegram_id=tid,ref=ref,kind=kind,delta=delta));db.flush()
    return True


def add_credits(db, tid, n, ref, kind='purchase'):
    """شراء أو إضافة من الإدارة: n توصية زيادة على الرصيد."""
    cust=db.get(Customer,tid)
    c=credit(db,tid)
    if not _ledger(db,tid,ref,kind,n):return c.balance
    c.balance+=n;c.mode='paid';c.updated_at=utcnow()
    if cust:cust.status='active'
    return c.balance


def start_trial(db, customer):
    c=credit(db,customer.telegram_id)
    c.mode='trial';c.trial_wins=0;c.updated_at=utcnow()
    customer.trial_used=True;customer.status='active'


def apply_signal_event(db, kind, signal_id, recipients):
    """خصم/تعويض الرصيد حسب الحدث. بيرجّع العملا اللي رصيدهم وصل ٢ (للتنبيه).
    open: خصم توصية من الرصيد المدفوع (المجاني في التجربة).
    cancelled: التوصية ما اتفعلتش ← ترجع للرصيد.
    stop: التوصية خسرت ← تعويض بتوصية.
    target1: في التجربة ← تتحسب توصية ناجحة."""
    low=[]
    for tid in set(recipients):
        c=credit(db,tid)
        charged=bool(db.scalar(select(CreditLedger.id).where(CreditLedger.telegram_id==tid,CreditLedger.ref==signal_id,
                                                             CreditLedger.kind=='charge')))
        if kind=='open':
            if c.balance>0 and _ledger(db,tid,signal_id,'charge',-1):
                c.balance-=1
                if c.balance==2:low.append(tid)
        elif kind=='cancelled':
            if charged and _ledger(db,tid,signal_id,'refund_cancel',1):c.balance+=1
            elif not charged and c.mode=='trial':_ledger(db,tid,signal_id,'trial_cancel',0)
        elif kind=='stop':
            if charged and _ledger(db,tid,signal_id,'refund_stop',1):c.balance+=1
        elif kind=='target1':
            if not charged and c.mode=='trial' and _ledger(db,tid,signal_id,'trial_win',0):c.trial_wins+=1
        c.updated_at=utcnow()
    return low

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
