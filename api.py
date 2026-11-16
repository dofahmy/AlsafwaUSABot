"""Private event ingestion endpoint and external payment confirmation endpoint.
No payment link or card form is exposed in Telegram.
"""
import os, json, hmac
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from common import Session, Customer, Event, Delivery, Payment, queue_recent_closed, grant, active, utcnow
from telegram_api import send

app=FastAPI(title='Rajih Customer API',docs_url=None,redoc_url=None)

def check(secret,expected):
    if not expected or not hmac.compare_digest(secret or '',expected): raise HTTPException(401,'Unauthorized')

class Incoming(BaseModel):
    event_key:str=Field(min_length=1,max_length=200)
    signal_id:str=Field(min_length=1,max_length=120)
    symbol:str=Field(min_length=1,max_length=32)
    kind:str=Field(pattern='^(open|target1|target2|stop|eod)$')
    message:str=Field(min_length=1,max_length=3000)

@app.get('/health')
def health(): return {'ok':True}

@app.post('/events')
def create_event(event:Incoming,x_bridge_key:str|None=Header(None)):
    check(x_bridge_key,os.getenv('BRIDGE_SECRET'))
    with Session.begin() as db:
        if db.scalar(select(Event.id).where(Event.event_key==event.event_key)):
            return {'ok':True,'duplicate':True}
        row=Event(event_key=event.event_key,signal_id=event.signal_id,symbol=event.symbol,
                  kind=event.kind,payload=event.message)
        db.add(row);db.flush()
        # An update goes only to recipients of the initial signal. New subscribers don't receive historical alerts.
        if event.kind=='open':
            targets=db.scalars(select(Customer).where(Customer.status=='active')).all()
            ids=[c.telegram_id for c in targets if active(c)]
        else:
            ids=db.scalars(select(Delivery.telegram_id).join(Event,Event.id==Delivery.event_id)
                 .where(Event.signal_id==event.signal_id,Event.kind=='open',Delivery.status=='sent')).all()
        for tid in set(ids): db.add(Delivery(event_id=row.id,telegram_id=tid))
        return {'ok':True,'queued':len(set(ids))}

class ExternalConfirmation(BaseModel):
    provider:str=Field(min_length=1,max_length=40)
    provider_payment_id:str=Field(min_length=1,max_length=180)
    telegram_id:int
    period_days:int=Field(ge=1,le=366)

@app.post('/internal/confirmed-payment')
def confirmed_payment(x:ExternalConfirmation,x_payment_bridge_key:str|None=Header(None)):
    # Trusted payment backend only: it must verify processor webhooks/signatures AND amounts
    # before calling this endpoint. Never call based on a user claim or screenshot.
    check(x_payment_bridge_key,os.getenv('PAYMENT_BRIDGE_SECRET'))
    with Session.begin() as db:
        if db.scalar(select(Payment.id).where(Payment.provider_payment_id==x.provider_payment_id)):
            return {'ok':True,'duplicate':True}
        customer=db.get(Customer,x.telegram_id)
        if not customer: raise HTTPException(404,'Customer not registered')
        db.add(Payment(provider=x.provider,provider_payment_id=x.provider_payment_id,
                       telegram_id=x.telegram_id,period_days=x.period_days))
        until=grant(db,customer,x.period_days,'subscription')
        queue_recent_closed(db,x.telegram_id,int(os.getenv('HISTORY_ON_ACTIVATION_COUNT','5')))
        for req in db.scalars(select(__import__('common').Request).where(__import__('common').Request.telegram_id==x.telegram_id,
                                                __import__('common').Request.kind=='subscription',
                                                __import__('common').Request.status=='pending')).all():
            req.status='approved';req.decided_at=utcnow()
    try: send(x.telegram_id,f'✅ تم تفعيل اشتراك راجح حتى {until:%Y-%m-%d} (UTC).')
    except Exception: pass
    return {'ok':True,'active_until':until.isoformat()}
