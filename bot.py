"""Telegram long polling bot. No purchases, checkout links, or payment verification via chat."""
import os, time, json, logging
from datetime import datetime, timezone
from sqlalchemy import select
from common import Session, Customer, Request, Event, Delivery, grant, active, utcnow
from telegram_api import call,send
LOG=logging.getLogger('rajih.bot')
logging.basicConfig(level=logging.INFO)
ADMINS={int(s) for s in os.getenv('ADMIN_TELEGRAM_IDS','').replace(' ','').split(',') if s.isdigit()}
TRIAL_DAYS=int(os.getenv('TRIAL_DAYS','3'))
PERIOD_DAYS=int(os.getenv('MANUAL_SUBSCRIPTION_DAYS','30'))
MAIN_MENU=[[{'text':'🎁 طلب تفعيل تجربة','callback_data':'request:trial'}, {'text':'📝 طلب تفعيل اشتراك','callback_data':'request:subscription'}],
           [{'text':'📈 توصياتي','callback_data':'mine'}, {'text':'👤 حسابي','callback_data':'account'}]]

def ensure_user(tid,username=''):
    with Session.begin() as db:
        c=db.get(Customer,tid)
        if not c: db.add(Customer(telegram_id=tid,username=username))
        elif username and c.username!=username:c.username=username

def account(tid):
    with Session() as db:
        c=db.get(Customer,tid)
        if not c or not active(c): return 'حالة الاشتراك: غير نشط. تقدري تطلبي تجربة أو تفعيل اشتراك من القائمة.'
        return f'✅ اشتراكك نشط حتى {c.access_until:%Y-%m-%d %H:%M} UTC.'

def mine(tid):
    with Session() as db:
        c=db.get(Customer,tid)
        if not active(c):return 'التوصيات الخاصة متاحة للمشتركين النشطين فقط.'
        ids=list(db.scalars(select(Event.signal_id).join(Delivery,Delivery.event_id==Event.id)
                     .where(Delivery.telegram_id==tid,Event.kind=='open',Delivery.status=='sent')
                     .order_by(Event.id.desc()).limit(15)).all())
        if not ids:return 'لا توجد توصيات خاصة مستلمة على حسابك حتى الآن.'
        out=[]
        for sid in ids[:10]:
            events=db.scalars(select(Event).where(Event.signal_id==sid).order_by(Event.id.desc()).limit(1)).all()
            if events:out.append(f'• {events[0].symbol}: {events[0].kind} — {sid}')
        return 'آخر توصيات حسابك:\n'+'\n'.join(out)

def submit_request(tid,kind):
    with Session.begin() as db:
        c=db.get(Customer,tid)
        if active(c) and kind=='trial':return 'اشتراكك نشط بالفعل.'
        if kind=='trial' and c.trial_used:return 'تم استخدام التجربة من قبل.'
        existing=db.scalar(select(Request.id).where(Request.telegram_id==tid,Request.kind==kind,Request.status=='pending'))
        if existing:return 'طلبك قيد المراجعة بالفعل.'
        req=Request(telegram_id=tid,kind=kind);db.add(req);db.flush();rid=req.id
    label='تجربة' if kind=='trial' else 'اشتراك'
    for admin in ADMINS:
        try:send(admin,f'📨 طلب تفعيل {label}\nالعميل: {tid}\nالطلب: #{rid}',
                 [[{'text':'✅ موافقة','callback_data':f'approve:{rid}'},{'text':'❌ رفض','callback_data':f'reject:{rid}'}]])
        except Exception:LOG.exception('Cannot notify admin')
    return f'✅ وصل طلب تفعيل {label} رقم #{rid}. هيوصلك إشعار لما تتم مراجعته.\nلا ترسلي بيانات بطاقتك داخل تليجرام.'

def decide(actor,action,rid):
    if actor not in ADMINS:return 'غير مصرح'
    with Session.begin() as db:
        req=db.get(Request,rid)
        if not req or req.status!='pending':return 'الطلب تمت معالجته أو غير موجود.'
        c=db.get(Customer,req.telegram_id)
        if action=='approve':
            if req.kind=='trial' and (c.trial_used or active(c)):return 'التجربة غير متاحة لهذا الحساب.'
            # Subscription approval is a manual administrative attestation of external payment.
            until=grant(db,c,TRIAL_DAYS if req.kind=='trial' else PERIOD_DAYS,req.kind)
            req.status='approved'
            notice=f'✅ تم تفعيل {"التجربة" if req.kind=="trial" else "الاشتراك"} حتى {until:%Y-%m-%d} UTC.'
        else:
            req.status='rejected';notice='تمت مراجعة طلب التفعيل، ولم تتم الموافقة عليه.'
        req.decided_at=utcnow();target=req.telegram_id
    try:send(target,notice)
    except Exception:LOG.exception('Notification failed')
    return 'تمت معالجة الطلب.'

def interact(uid,username,data):
    ensure_user(uid,username)
    if data.startswith('approve:') or data.startswith('reject:'):
        action,rid=data.split(':',1)
        if not rid.isdigit():return 'طلب غير صحيح.'
        return decide(uid,action,int(rid))
    if data=='request:trial':return submit_request(uid,'trial')
    if data=='request:subscription':return submit_request(uid,'subscription')
    if data=='mine':return mine(uid)
    if data=='account':return account(uid)
    return 'اختاري من القائمة.'

def process(update):
    msg=update.get('message')
    cb=update.get('callback_query')
    if cb:
        uid=cb['from']['id'];username=cb['from'].get('username','')
        response=interact(uid,username,cb.get('data',''))
        call('answerCallbackQuery',{'callback_query_id':cb['id'],'text':response[:190],'show_alert':False})
        send(uid,response,MAIN_MENU)
        return
    if msg and msg['chat']['type']=='private':
        uid=msg['from']['id'];ensure_user(uid,msg['from'].get('username',''))
        send(uid,'أهلًا بيكي في راجح 👑\nمفيش شراء أو دفع داخل تليجرام. هنا بتقدمي طلب تفعيل وبتتابعي توصياتك.',MAIN_MENU)

def deliver():
    with Session() as db:
        ids=list(db.scalars(select(Delivery.id).where(Delivery.status=='queued').order_by(Delivery.id).limit(35)).all())
    for did in ids:
        with Session() as db:
            d=db.get(Delivery,did);ev=db.get(Event,d.event_id);c=db.get(Customer,d.telegram_id)
            if not c or not active(c):
                with Session.begin() as write:
                    row=write.get(Delivery,did);row.status='skipped'
                continue
            message=ev.payload
        try:
            send(d.telegram_id,message)
            with Session.begin() as write:
                row=write.get(Delivery,did);row.status='sent';row.sent_at=utcnow()
            time.sleep(.07)
        except Exception as e:
            LOG.warning('deliver %s: %s',did,str(e)[:100])
            # Retry later: no at-most-once guarantee if Telegram accepted just before timeout.
            break

def main():
    if not os.getenv('CUSTOMER_BOT_TOKEN'):raise RuntimeError('Set CUSTOMER_BOT_TOKEN')
    call('deleteWebhook',{'drop_pending_updates':False})
    offset=None
    while True:
        try:
            deliver()
            updates=call('getUpdates',{'offset':offset,'timeout':10,'allowed_updates':['message','callback_query']},timeout=20)
            for u in updates:
                offset=u['update_id']+1
                try:process(u)
                except Exception:LOG.exception('Failed to process update')
        except Exception:
            LOG.exception('Polling error');time.sleep(3)
if __name__=='__main__': main()
