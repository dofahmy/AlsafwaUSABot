"""Private event ingestion endpoint and external payment confirmation endpoint.
No payment link or card form is exposed in Telegram.
"""
import os, json, hmac, hashlib, base64, time, logging
from fastapi import FastAPI, Header, HTTPException, Request as HTTPRequest
from pydantic import BaseModel, Field
from sqlalchemy import select
from common import Session, Customer, Event, Delivery, Payment, PremarketPost, queue_recent_closed, grant, active, utcnow, eligible, apply_signal_event, add_credits, PACKAGE_SIZE
from telegram_api import send

app=FastAPI(title='Rajih Customer API',docs_url=None,redoc_url=None)

def check(secret,expected):
    if not expected or not hmac.compare_digest(secret or '',expected): raise HTTPException(401,'Unauthorized')

class Incoming(BaseModel):
    event_key:str=Field(min_length=1,max_length=200)
    signal_id:str=Field(min_length=1,max_length=120)
    symbol:str=Field(min_length=1,max_length=32)
    kind:str=Field(pattern='^(open|activated|cancelled|target1|target2|stop|eod|notice)$')
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
        # التوصية الجديدة: لكل عميل عنده رصيد أو في التجربة. المتابعة: لكل اللي استلموا التوصية نفسها.
        if event.kind=='open':
            ids=[c.telegram_id for c in db.scalars(select(Customer)).all() if eligible(db,c.telegram_id)]
        else:
            ids=db.scalars(select(Delivery.telegram_id).join(Event,Event.id==Delivery.event_id)
                 .where(Event.signal_id==event.signal_id,Event.kind=='open')).all()
        ids=sorted(set(ids))
        for tid in ids: db.add(Delivery(event_id=row.id,telegram_id=tid))
        low=apply_signal_event(db,event.kind,event.signal_id,ids)
    for tid in low:                     # تنبيه: فاضل توصيتين في الرصيد
        try:send(tid,'🔔 تبقى في رصيدك توصيتان. يمكنك إضافة باقة جديدة في أي وقت من «💳 اشترك الآن».')
        except Exception: pass
    return {'ok':True,'queued':len(ids)}

class PremarketIn(BaseModel):
    day:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    caption:str=Field(min_length=1,max_length=1024)
    image_b64:str=Field(min_length=10,max_length=8_000_000)

@app.post('/premarket')
def create_premarket(p:PremarketIn,x_bridge_key:str|None=Header(None)):
    """الماسح بيبعت صورة «نظرة قبل الافتتاح»؛ البوت بيعرض آخر واحدة لما العميل يدوس الزرار."""
    check(x_bridge_key,os.getenv('BRIDGE_SECRET'))
    with Session.begin() as db:
        db.add(PremarketPost(day=p.day,caption=p.caption,image_b64=p.image_b64))
        db.flush()
        old=db.scalars(select(PremarketPost).order_by(PremarketPost.id.desc()).offset(5)).all()
        for o in old: db.delete(o)
    return {'ok':True}

class ExternalConfirmation(BaseModel):
    provider:str=Field(min_length=1,max_length=40)
    provider_payment_id:str=Field(min_length=1,max_length=180)
    telegram_id:int
    period_days:int=Field(ge=1,le=1000)     # عدد التوصيات المضافة

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
        bal=add_credits(db,x.telegram_id,x.period_days,f'{x.provider}:{x.provider_payment_id}')
        for req in db.scalars(select(__import__('common').Request).where(__import__('common').Request.telegram_id==x.telegram_id,
                                                __import__('common').Request.kind=='subscription',
                                                __import__('common').Request.status=='pending')).all():
            req.status='approved';req.decided_at=utcnow()
    try: send(x.telegram_id,f'✅ تمت إضافة {x.period_days} توصية إلى رصيدك. رصيدك الآن: {bal} توصية.')
    except Exception: pass
    return {'ok':True,'balance':bal}


# ---------------- Whop: الدفع بيضيف رصيد لوحده ----------------
LOG=logging.getLogger('safwa.whop')
ADMINS=[int(x) for x in os.getenv('ADMIN_TELEGRAM_IDS','').replace(' ','').split(',') if x.isdigit()]
PACKAGE_PRICE=float(os.getenv('PACKAGE_PRICE','50'))


def _whop_verify(body:bytes, headers)->bool:
    """توقيع Standard Webhooks: v1,base64(HMAC-SHA256(secret, id.timestamp.body))."""
    secret=os.getenv('WHOP_WEBHOOK_SECRET','')
    if not secret:return False
    wid,ts,sigs=headers.get('webhook-id'),headers.get('webhook-timestamp'),headers.get('webhook-signature','')
    if not (wid and ts and sigs):return False
    try:
        if abs(time.time()-int(ts))>600:return False
    except ValueError:return False
    signed=f'{wid}.{ts}.'.encode()+body
    keys=[secret.encode()]
    raw=secret.split('_',1)[1] if secret.startswith(('whsec_','ws_')) else secret
    try:keys.append(base64.b64decode(raw+'='*(-len(raw)%4)))
    except Exception:pass
    keys.append(raw.encode())
    expected={base64.b64encode(hmac.new(k,signed,hashlib.sha256).digest()).decode() for k in keys}
    for part in sigs.split():
        v=part.split(',',1)[-1]
        if v in expected:return True
    return False


def _find(d,*keys):
    """أول قيمة موجودة من أكتر من مكان محتمل في رسالة Whop."""
    for k in keys:
        cur=d
        for p in k.split('.'):
            cur=cur.get(p) if isinstance(cur,dict) else None
            if cur is None:break
        if cur not in (None,'',{}):return cur
    return None


def _admins(text):
    if not ADMINS:print('WHOP: ADMIN_TELEGRAM_IDS فاضي في خدمة الـ API',flush=True)
    for a in ADMINS:
        try:send(a,text)
        except Exception as exc:print('WHOP: رسالة الإدارة ما اتبعتتش:',exc,flush=True)


@app.post('/whop/webhook')
async def whop_webhook(req:HTTPRequest):
    body=await req.body()
    hdr={k.lower():v for k,v in req.headers.items()}
    print('WHOP webhook وصل:',body[:1500].decode('utf-8','replace'),flush=True)
    if not _whop_verify(body,hdr):
        print('WHOP webhook: التوقيع غلط — راجعي WHOP_WEBHOOK_SECRET',
              {k:hdr.get(k) for k in ('webhook-id','webhook-timestamp')},flush=True)
        _admins('⚠️ Whop بعت إشعار دفع بس التوقيع ما اتطابقش. راجعي WHOP_WEBHOOK_SECRET في خدمة الـ API.')
        raise HTTPException(401,'bad signature')
    ev=json.loads(body or b'{}')
    kind=ev.get('type') or ev.get('action') or ''
    data=ev.get('data') or {}
    LOG.info('whop %s: %s',kind,json.dumps(ev)[:2000])
    if kind!='payment.succeeded':
        return {'ok':True,'ignored':kind}
    pay_id=str(_find(data,'id') or ev.get('id') or '')
    tid=_find(data,'metadata.telegram_id','checkout_configuration.metadata.telegram_id','membership.metadata.telegram_id')
    qty=_find(data,'quantity','line_items.0.quantity')
    amount=_find(data,'subtotal','total','final_amount','amount','usd_total')
    # المبلغ ممكن ييجي بعملة العميل (زي الجنيه)، فما بنعتمدش عليه: الكمية لو موجودة، وإلا باقة واحدة
    try:packages=max(1,int(qty)) if qty else 1
    except (TypeError,ValueError):packages=1
    n=packages*PACKAGE_SIZE
    email=_find(data,'user.email','member.email','email') or ''
    if not tid or not str(tid).isdigit():
        _admins(f'💰 دفعة جديدة على Whop من غير ID تيليجرام\nالمبلغ: {amount}\nالإيميل: {email}\nرقم الدفعة: {pay_id}\n'
                f'أضيفي الرصيد يدويًا: /add ID_العميل {n}')
        return {'ok':True,'unmatched':True}
    tid=int(tid)
    with Session.begin() as db:
        if db.scalar(select(Payment.id).where(Payment.provider_payment_id==f'whop:{pay_id}')):
            return {'ok':True,'duplicate':True}
        if not db.get(Customer,tid):db.add(Customer(telegram_id=tid));db.flush()
        db.add(Payment(provider='whop',provider_payment_id=f'whop:{pay_id}',telegram_id=tid,period_days=n))
        bal=add_credits(db,tid,n,f'whop:{pay_id}')
    try:send(tid,f'✅ تم استلام الدفع بنجاح، وأُضيفت {n} توصية إلى رصيدك.\nرصيدك الآن: {bal} توصية. ستصلك التوصيات الجديدة فور صدورها 👑')
    except Exception as exc:print('WHOP: رسالة العميل ما اتبعتتش:',exc,flush=True)
    _admins(f'💰 دفعة Whop: العميل {tid} — {packages} باقة ({n} توصية). رصيده الآن {bal}.\nالمبلغ: {amount} — {email}')
    return {'ok':True,'credited':n}
