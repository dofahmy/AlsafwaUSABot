"""Telegram long polling bot. No purchases, checkout links, or payment verification via chat."""
import os, time, json, logging
from datetime import datetime, timezone
from sqlalchemy import select
from common import (Session, Customer, Request, Event, Delivery, HistoricalDelivery, PremarketPost, queue_recent_closed,
                    grant, active, utcnow, Credit, credit, eligible, start_trial, add_credits, has_purchased, TRIAL_WINS, PACKAGE_SIZE)
from telegram_api import call,send,send_photo,send_document
LOG=logging.getLogger('rajih.bot')
logging.basicConfig(level=logging.INFO)
ADMINS={int(s) for s in os.getenv('ADMIN_TELEGRAM_IDS','').replace(' ','').split(',') if s.isdigit()}
TRIAL_DAYS=int(os.getenv('TRIAL_DAYS','3'))
TRIAL_SESSIONS=int(os.getenv('TRIAL_SESSIONS','2'))     # التجربة المجانية = عدد جلسات تداول
from datetime import timedelta
from zoneinfo import ZoneInfo
NY=ZoneInfo('America/New_York')
# أجازات البورصة الأمريكية (السوق مقفول طول اليوم)
US_HOLIDAYS={'2026-11-26','2026-12-25','2027-01-01','2027-01-18','2027-02-15','2027-03-26','2027-05-31',
             '2027-06-18','2027-07-05','2027-09-06','2027-11-25','2027-12-24'}


def _is_session(d):
    return d.weekday()<5 and d.strftime('%Y-%m-%d') not in US_HOLIDAYS


def trial_until(now_utc=None,sessions=None):
    """نهاية التجربة: آخر الجلسة رقم N (الساعة ٨ بالليل نيويورك، بعد التداول بعد الإغلاق).
    لو التفعيل قبل افتتاح السوق (٩:٣٠ نيويورك) في يوم تداول، اليوم ده بيتحسب أول جلسة."""
    sessions=sessions or TRIAL_SESSIONS
    now=(now_utc or utcnow()).astimezone(NY)
    d=now.date()
    if not (_is_session(d) and (now.hour,now.minute)<(9,30)):
        d+=timedelta(days=1)
    n=0
    while True:
        if _is_session(d):
            n+=1
            if n==sessions:break
        d+=timedelta(days=1)
    return datetime(d.year,d.month,d.day,20,0,tzinfo=NY).astimezone(timezone.utc)

PERIOD_DAYS=int(os.getenv('MANUAL_SUBSCRIPTION_DAYS','30'))
WHOP_CHECKOUT_URL=(os.getenv('WHOP_CHECKOUT_URL') or 'https://whop.com/checkout/plan_iD20tgprZcehh').strip()   # باقة ١٠ توصيات
WHOP_PLAN_ID=os.getenv('WHOP_PLAN_ID') or WHOP_CHECKOUT_URL.rstrip('/').split('/')[-1]
WHOP_API_KEY=os.getenv('WHOP_API_KEY','').strip()
# مع مفتاح Whop: الزرار بيعمل لكل عميل صفحة دفع خاصة فيها الـ ID بتاعه، علشان الرصيد يتضاف لوحده بعد الدفع
SUBSCRIBE_BTN=({'text':'💳 اشترك الآن','callback_data':'buy'} if WHOP_API_KEY else
               {'text':'💳 اشترك الآن','url':WHOP_CHECKOUT_URL} if WHOP_CHECKOUT_URL else
               {'text':'💳 اشترك الآن','callback_data':'request:subscription'})


def whop_link(tid):
    """صفحة دفع Whop خاصة بالعميل (الـ ID بتاعه في metadata). لو فشلت ← الرابط العام."""
    import requests as rq
    if not WHOP_API_KEY:return WHOP_CHECKOUT_URL
    head={'Authorization':f'Bearer {WHOP_API_KEY}','Content-Type':'application/json'}
    meta={'telegram_id':str(tid)}
    for body in ({'variant_id':WHOP_PLAN_ID,'metadata':meta},{'plan_id':WHOP_PLAN_ID,'metadata':meta}):
        try:
            r=rq.post('https://api.whop.com/api/v1/checkout_configurations',json=body,headers=head,timeout=20)
            if r.ok and r.json().get('purchase_url'):return r.json()['purchase_url']
            LOG.warning('whop checkout %s: %s',r.status_code,r.text[:300])
        except Exception as exc:LOG.warning('whop checkout failed: %s',exc)
    return WHOP_CHECKOUT_URL


def send_buy(uid):
    url=whop_link(uid)
    price=os.getenv('PACKAGE_PRICE','50')
    send(uid,f'💳 باقة {PACKAGE_SIZE} توصيات — {price} دولار\nيمكنك شراء أكثر من باقة، ويُضاف رصيدها تلقائيًا بعد الدفع.\n'
             'اضغط الزر التالي لإتمام الدفع بأمان عبر Whop.',[[{'text':'💳 ادفع الآن','url':url}]])
MAIN_MENU=[[{'text':f'🎁 تجربة مجانية ({TRIAL_WINS} توصيات ناجحة)','callback_data':'request:trial'}, SUBSCRIBE_BTN],
           [{'text':'📈 توصياتي','callback_data':'mine'}, {'text':'👤 حسابي','callback_data':'account'}],
           [{'text':'☀️ نظرة قبل الافتتاح','callback_data':'pre'}, {'text':'🎧 خدمة العملاء','callback_data':'contact'}]]

# قائمة الأوامر (زرار Menu جنب خانة الكتابة) — بتتسجل في تيليجرام أول ما البوت يشتغل
BOT_COMMANDS=[
    {'command':'start','description':'بدء الاستخدام'},
    {'command':'trial','description':'تفعيل التجربة المجانية'},
    {'command':'subscribe','description':'الاشتراك الآن'},
    {'command':'mine','description':'توصياتي'},
    {'command':'account','description':'بيانات اشتراكي'},
    {'command':'premarket','description':'نظرة قبل الافتتاح'},
    {'command':'contact','description':'خدمة العملاء'},
]
SUPPORT_USERNAME=os.getenv('SUPPORT_USERNAME','').lstrip('@')

# الكلام اللي بيظهر لأي حد يفتح البوت قبل ما يدوس «ابدأ» (تحت صورة المدخل)
BOT_DESCRIPTION="""👑 الصفوة | توصيات لحظية على الأسهم الأمريكية

🎯 توصيات بسعر شراء وهدفين ووقف خسارة واضح
⚡ تنبيهات فورية عند التفعيل وتحقق الأهداف أو الوقف
☀️ نظرة يومية قبل افتتاح السوق الأمريكي
🛡️ مستوى مخاطرة يومي وإدارة واضحة للمراكز

🎁 تجربة مجانية حتى تحقيق 5 توصيات ناجحة
اضغط «ابدأ» وفعّل تجربتك الآن"""
# السطر اللي بيظهر في صفحة البوت (About) ولما حد يشارك البوت
BOT_SHORT_DESCRIPTION="توصيات لحظية على الأسهم الأمريكية بأهداف ووقف خسارة واضح — جرّبها مجانًا حتى 5 توصيات ناجحة"   # يوزر حساب خدمة العملاء (اختياري)

def ensure_user(tid,username=''):
    with Session.begin() as db:
        c=db.get(Customer,tid)
        if not c: db.add(Customer(telegram_id=tid,username=username))
        elif username and c.username!=username:c.username=username

def account(tid):
    with Session() as db:
        cr=db.get(Credit,tid)
        if not cr or not eligible(db,tid):
            return 'حالة الاشتراك: غير نشط. يمكنك بدء التجربة المجانية أو الاشتراك من القائمة.'
        lines=['👤 بيانات اشتراكك']
        if cr.balance>0:lines.append(f'💳 رصيدك: {_count(cr.balance,"توصية","توصيات")}')
        if cr.mode=='trial' and cr.trial_wins<TRIAL_WINS:
            lines.append(f'🎁 التجربة المجانية: {cr.trial_wins} من {TRIAL_WINS} توصيات ناجحة')
        lines.append('\nℹ️ التوصية التي تضرب وقف الخسارة نعوّضك عنها بتوصية، والتوصية التي لا تُفعَّل لا تُحتسب.')
        return '\n'.join(lines)

KIND_AR={
    'open':'🟢 مفتوحة — لم تصل إلى هدف أو وقف بعد',
    'target1':'🎯 حققت الهدف الأول',
    'target2':'🎯🎯 حققت الهدف الثاني',
    'stop':'⛔ ضربت وقف الخسارة',
    'eod':'🔔 تم إغلاقها نهاية اليوم دون هدف أو وقف',
    'activated':'🟢 تم تفعيلها — لم تصل إلى هدف أو وقف بعد',
    'cancelled':'🚫 ملغية لعدم تفعيل سعر الشراء',
}


def _entry_price(text):
    import re
    m=re.search(r'سعر (?:الدخول|الشراء):\s*([0-9.]+)',text or '')
    return m.group(1) if m else ''


def _pct(text):
    """النسبة اللي في رسالة الحدث، زي (+11.93%) أو (-12.00%)."""
    import re
    m=re.findall(r'\(([+\-−]?[0-9]+(?:\.[0-9]+)?)%\)',text or '')
    if not m:return None
    try:return float(m[-1].replace('−','-'))
    except ValueError:return None


def _num(pattern,text):
    import re
    m=re.search(pattern,text or '')
    try:return float(m.group(1)) if m else None
    except ValueError:return None


def _pct_from_levels(open_text,ev):
    """لو رسالة الحدث مافيهاش نسبة (من نسخة قديمة للماسح): نحسبها من أسعار رسالة التوصية."""
    entry=_num(r'سعر (?:الدخول|الشراء):\s*([0-9.]+)',open_text)
    if not entry:return None
    if ev.kind=='target1':p=_num(r'الهدف الأول:\s*([0-9.]+)',open_text)
    elif ev.kind=='target2':p=_num(r'الهدف الثاني:\s*([0-9.]+)',open_text)
    elif ev.kind=='stop':p=_num(r'وقف الخسارة:\s*([0-9.]+)',open_text)
    else:p=_num(r'آخر سعر\s*([0-9.]+)',ev.payload)
    return round((p/entry-1)*100,2) if p else None


def _count(n,one,many):
    if n==1:return f'{one} واحدة'
    if n==2:return {'توصية':'توصيتان','جلسة':'جلستين'}.get(one,f'2 {one}')
    return f'{n} {many if 3<=n<=10 else one}'


def _fmt_day(d):
    return d.strftime('%d/%m')


EXPIRED_COMMANDS=[{'command':'start','description':'بدء الاستخدام'},{'command':'subscribe','description':'اشترك الآن'}]


def subscribe_kb():
    return [[SUBSCRIBE_BTN]]


def support_kb():
    """زرار خدمة العملاء: بيفتح محادثة حساب الدعم لو SUPPORT_USERNAME متحدد."""
    if SUPPORT_USERNAME:
        return [[{'text':'💬 خدمة العملاء','url':f'https://t.me/{SUPPORT_USERNAME}'}]]
    return [[{'text':'💬 خدمة العملاء','callback_data':'contact'}]]


def _paid_before(db,tid):
    return has_purchased(db,tid)


def expired_state(uid):
    """None لو العميل جديد أو اشتراكه شغال؛ 'trial' لو تجربته خلصت؛ 'sub' لو اشتراكه المدفوع خلص."""
    if uid in ADMINS:return None
    with Session() as db:
        c=db.get(Customer,uid)
        if not c or eligible(db,uid):return None
        cr=db.get(Credit,uid)
        if not cr or cr.mode=='none':return None              # عميل جديد ما بدأش لسه
        return 'sub' if _paid_before(db,uid) else 'trial'


def ended_short(state):
    return 'انتهت فترة التجربة المجانية' if state=='trial' else 'انتهى رصيدك من التوصيات'


def set_expired_commands(uid,expired):
    """العميل اللي اشتراكه خلص: قايمة الأوامر بتاعته بتبقى «بدء الاستخدام» و«خدمة العملاء» بس."""
    try:
        if expired:call('setMyCommands',{'commands':EXPIRED_COMMANDS,'scope':{'type':'chat','chat_id':uid}})
        else:call('deleteMyCommands',{'scope':{'type':'chat','chat_id':uid}})
    except Exception:LOG.warning('chat commands for %s failed',uid)


def check_expiries():
    """كل دقيقة: أي تجربة أو اشتراك خلص ← رسالة للعميل، والقوايم تختفي ويفضل زرار خدمة العملاء بس."""
    with Session.begin() as db:
        ended,back=[],[]
        for c in db.scalars(select(Customer).where(Customer.status.in_(('active','expired')))).all():
            ok=eligible(db,c.telegram_id)
            cr=db.get(Credit,c.telegram_id)
            if c.status=='active' and not ok and cr and cr.mode!='none':
                c.status='expired';ended.append((c.telegram_id,_paid_before(db,c.telegram_id)))
            elif c.status=='expired' and ok:                  # رجعله رصيد (تعويض أو شراء)
                c.status='active';back.append(c.telegram_id)
    for tid in back:
        set_expired_commands(tid,False)
        try:send(tid,'✅ تمت إضافة رصيد إلى حسابك، وستصلك التوصيات الجديدة فور صدورها.',MAIN_MENU)
        except Exception:pass
    for tid,paid in ended:
        msg=('انتهى رصيدك من التوصيات. يسعدنا استمرارك معنا؛ اضغط «اشترك الآن» لإضافة باقة جديدة.' if paid else
             'انتهت فترتك التجريبية. يسعدنا انضمامك إلى مشتركي الصفوة؛ اضغط «اشترك الآن» لإتمام الاشتراك.')
        try:send(tid,msg,subscribe_kb())
        except Exception:LOG.warning('expiry notice to %s failed',tid)
        set_expired_commands(tid,True)
        who,_=_who(tid)
        _notify_admins(f'⏰ {"انتهى رصيد" if paid else "انتهت تجربة"} العميل {who} (ID: {tid})')


def send_customers(admin):
    """كل العملا: الـ ID واليوزر والحالة والرصيد — رسالة ملخص + ملف CSV فيه كل التفاصيل."""
    import csv,io
    with Session() as db:
        custs=db.scalars(select(Customer).order_by(Customer.created_at)).all()
        rows=[]
        for c in custs:
            cr=db.get(Credit,c.telegram_id)
            mode=cr.mode if cr else 'none'
            ok=eligible(db,c.telegram_id)
            state=('مشترك' if mode=='paid' and ok else 'في التجربة' if mode=='trial' and ok else
                   'رصيده خلص' if mode=='paid' else 'تجربته خلصت' if mode=='trial' else 'جديد')
            rows.append({'ID':c.telegram_id,'username':('@'+c.username) if c.username else '',
                         'الحالة':state,'الرصيد':cr.balance if cr else 0,
                         'نجاحات التجربة':f'{cr.trial_wins}/{TRIAL_WINS}' if cr and mode=='trial' else '',
                         'اشترى قبل كده':'نعم' if has_purchased(db,c.telegram_id) else '',
                         'تاريخ الدخول':c.created_at.astimezone(NY).strftime('%Y-%m-%d') if c.created_at else ''})
    if not rows:
        send(admin,'لا يوجد عملاء بعد.');return
    from collections import Counter
    cnt=Counter(r['الحالة'] for r in rows)
    head=f'👥 العملاء: {len(rows)}\n'+' — '.join(f'{k}: {v}' for k,v in cnt.items())
    lines=[f"{r['ID']} {r['username']} — {r['الحالة']}"+(f" ({r['الرصيد']})" if r['الرصيد'] else '') for r in rows]
    chunk=head+'\n\n'
    for ln in lines:
        if len(chunk)+len(ln)>3800:
            send(admin,chunk);chunk=''
        chunk+=ln+'\n'
    if chunk.strip():send(admin,chunk)
    buf=io.StringIO();w=csv.DictWriter(buf,fieldnames=list(rows[0].keys()));w.writeheader();w.writerows(rows)
    try:send_document(admin,buf.getvalue().encode('utf-8-sig'),'safwa_customers.csv','كل العملاء بالتفاصيل')
    except Exception:LOG.exception('customers csv failed')


def contact(uid):
    """خدمة العملاء: زرار يفتح محادثة حساب الدعم، أو أي رسالة يكتبها العميل بتتحول للإدارة."""
    expired=expired_state(uid)
    if SUPPORT_USERNAME:
        send(uid,'للتواصل مع خدمة عملاء الصفوة، اضغط الزر التالي لفتح المحادثة مع ممثلنا.',
             support_kb()+([] if expired else MAIN_MENU))
    else:
        send(uid,'🎧 خدمة العملاء\nاكتب رسالتك هنا مباشرة وسيتم تحويلها إلى فريق الصفوة، وسنرد عليك في أقرب وقت.',
             None if expired else MAIN_MENU)


def send_premarket(uid):
    """آخر «نظرة قبل الافتتاح»: الشارت وتحته التعليق، زي القناة بالظبط."""
    import base64
    with Session() as db:
        p=db.scalars(select(PremarketPost).order_by(PremarketPost.id.desc()).limit(1)).first()
    if not p:
        send(uid,'تُنشر يوميًا قبل افتتاح السوق الأمريكي.',MAIN_MENU);return
    today=utcnow().astimezone(NY).strftime('%Y-%m-%d')
    cap=p.caption if p.day==today else 'ℹ️ هذه آخر نظرة منشورة، وستُنشر نظرة اليوم قبل افتتاح السوق.\n\n'+p.caption
    try:send_photo(uid,base64.b64decode(p.image_b64),cap[:1024],MAIN_MENU)
    except Exception:
        LOG.exception('premarket photo failed');send(uid,cap,MAIN_MENU)


def mine(tid):
    with Session() as db:
        c=db.get(Customer,tid)
        if not active(c):return 'التوصيات الخاصة متاحة للمشتركين النشطين فقط.'
        ids=list(db.scalars(select(Event.signal_id).join(Delivery,Delivery.event_id==Event.id)
                     .where(Delivery.telegram_id==tid,Event.kind=='open',Delivery.status=='sent')
                     .order_by(Event.id.desc()).limit(15)).all())
        if not ids:return 'لا توجد توصيات خاصة مستلمة على حسابك حتى الآن.'
        out,rets=[],[]
        for sid in ids[:10]:
            evs=db.scalars(select(Event).where(Event.signal_id==sid).order_by(Event.id.desc())).all()
            if not evs:continue
            last=next((e for e in evs if e.kind in KIND_AR),evs[0])        # آخر حالة (من غير رسايل التنبيه)
            opened=next((e for e in evs if e.kind=='open'),None)
            parts=sid.split(':')
            day=parts[0] if parts else ''
            hhmm=':'.join(parts[2:4]) if len(parts)>=4 else ''
            try:day=datetime.strptime(day,'%Y-%m-%d').strftime('%d/%m')
            except Exception:pass
            price=_entry_price(opened.payload if opened else '')
            label=KIND_AR.get(last.kind,last.kind)
            if last.kind=='open' and '⏳' in (last.payload or ''):
                label='⏳ في انتظار التفعيل — لم ينزل السعر إلى سعر الشراء بعد'
            line=f'🔷 {last.symbol}\n{label}\n🗓️ {day}'
            if hhmm:line+=f' الساعة {hhmm} بتوقيت نيويورك'
            if price:line+=f' — سعر الشراء {price}'
            pct=(_pct(last.payload) or _pct_from_levels(opened.payload if opened else '',last)) if last.kind in ('target1','target2','stop','eod') else None
            if pct is not None:
                line+=f'\n💵 العائد: {pct:+.2f}%'
                rets.append(pct)
            out.append(line)
        tail=''
        if rets:
            wins=sum(r>0 for r in rets)
            tail=(f'\n\n📊 التوصيات المنتهية: {len(rets)} — الرابحة منها: {wins}'
                  f'\nمتوسط العائد: {sum(rets)/len(rets):+.2f}%')
        return 'آخر توصيات حسابك:\n\n'+'\n\n'.join(out)+tail

def _who(tid):
    with Session() as db:
        c=db.get(Customer,tid)
        u=(c.username or '') if c else ''
    return (f'@{u}' if u else 'ليس له اسم مستخدم'), u


def _notify_admins(text,keyboard=None):
    for admin in ADMINS:
        try:send(admin,text,keyboard)
        except Exception:LOG.exception('Cannot notify admin')


def auto_trial(tid):
    """التجربة المجانية بتتفعل على طول، مرة واحدة بس لكل حساب تيليجرام (بالـ ID)."""
    with Session.begin() as db:
        c=db.get(Customer,tid)
        if eligible(db,tid):
            return 'اشتراكك نشط بالفعل ✅'
        if c.trial_used:
            return 'لقد استخدمت التجربة المجانية من قبل 🙏\nإذا رغبت في الاستمرار معنا اضغط «💳 اشترك الآن».'
        start_trial(db,c)
        req=Request(telegram_id=tid,kind='trial',status='approved');req.decided_at=utcnow()
        db.add(req)
        queue_recent_closed(db,tid,int(os.getenv('HISTORY_ON_ACTIVATION_COUNT','5')))
    who,_=_who(tid)
    _notify_admins(f'🎁 تم تفعيل تجربة مجانية تلقائيًا\nالعميل: {who}\nالـ ID: {tid}')
    return (f'🎉 تم تفعيل التجربة المجانية: ستصلك التوصيات حتى تحقيق {TRIAL_WINS} توصيات ناجحة '
            '(التوصية الناجحة = وصلت للهدف الأول).\n'
            'ستصلك التوصيات الجديدة هنا فور صدورها.\n'
            'وبعد انتهاء التجربة يمكنك الاشتراك بباقات التوصيات من «💳 اشترك الآن».')


def subscribe_now(tid):
    """الاشتراك الآن: بيبعت للإدارة طلب تواصل واشتراك، والإدارة بتفعّل بعد الدفع."""
    with Session.begin() as db:
        existing=db.scalar(select(Request.id).where(Request.telegram_id==tid,Request.kind=='subscription',
                                                     Request.status=='pending'))
        if existing:
            rid,again=existing,True          # بنفكّر الإدارة بالطلب تاني
        else:
            req=Request(telegram_id=tid,kind='subscription');db.add(req);db.flush();rid,again=req.id,False
    who,u=_who(tid)
    kb=[[{'text':'✅ تفعيل الاشتراك','callback_data':f'approve:{rid}'},{'text':'❌ رفض','callback_data':f'reject:{rid}'}]]
    if u:kb.insert(0,[{'text':'💬 تواصل مع العميل','url':f'https://t.me/{u}'}])
    if not ADMINS:LOG.error('ADMIN_TELEGRAM_IDS فارغ — لن تصل طلبات الاشتراك لأحد')
    _notify_admins(f'💳 {"تذكير: " if again else ""}طلب اشتراك وتواصل\nالعميل: {who}\nالـ ID: {tid}\nالطلب: #{rid}'
                   + ('' if u else '\n⚠️ العميل ليس له اسم مستخدم، فلا يمكن التواصل معه عبر رابط. يمكن الرد عليه من البوت بأمر /r.'),kb)
    if again:
        return 'وصلنا طلبك من قبل ✅ وتم تذكير الفريق، وسنتواصل معك في أقرب وقت.'
    msg=('✅ وصل طلب الاشتراك، وسيتواصل معك فريق الصفوة هنا على تيليجرام في أقرب وقت لتوضيح تفاصيل الاشتراك والدفع.\n'
         'من فضلك لا ترسل بيانات بطاقتك داخل تليجرام.')
    if not u:
        msg+='\n\n📌 حسابك ليس له اسم مستخدم، ولكي نتمكن من التواصل معك: أضف اسم مستخدم من إعدادات تيليجرام، أو أرسل هنا رقم واتساب للتواصل.'
    return msg


def submit_request(tid,kind):
    return auto_trial(tid) if kind=='trial' else subscribe_now(tid)


DAY_CHOICES=[10,20,30,50,100]          # عدد التوصيات (باقة = ١٠)


def _days(n):
    return _count(n,'توصية','توصيات')


def ask_days(actor,rid):
    """لما الإدارة تدوس «تفعيل الاشتراك»: البوت بيسأل المدة الأول، وما بيفعّلش غير بعد اختيارها."""
    if actor not in ADMINS:return 'غير مصرح'
    with Session() as db:
        req=db.get(Request,rid)
        if not req or req.status!='pending':return 'الطلب تمت معالجته أو غير موجود.'
        tid=req.telegram_id
        cr=db.get(Credit,tid)
        cur_line=(f'\nرصيده الحالي: {_count(cr.balance,"توصية","توصيات")}، والعدد الجديد هيتضاف عليه.'
                  if cr and cr.balance>0 else '')
    who,_=_who(tid)
    rows=[]
    for i in range(0,len(DAY_CHOICES),4):
        rows.append([{'text':_days(d),'callback_data':f'grant:{rid}:{d}'} for d in DAY_CHOICES[i:i+4]])
    rows.append([{'text':'❌ إلغاء','callback_data':'noop'}])
    return (f'⏳ تفعيل اشتراك الطلب #{rid}\nالعميل: {who} (ID: {tid}){cur_line}\n\nاختر عدد التوصيات المضافة لرصيده.\n'
            f'أو لعدد آخر أرسل: /sub {rid} عدد_التوصيات\nمثال: /sub {rid} 40'),rows


def decide(actor,action,rid,days=None):
    if actor not in ADMINS:return 'غير مصرح'
    if action=='approve' and days is None:
        return ask_days(actor,rid)
    with Session.begin() as db:
        req=db.get(Request,rid)
        if not req or req.status!='pending':return 'الطلب تمت معالجته أو غير موجود.'
        c=db.get(Customer,req.telegram_id)
        if action=='approve':
            if req.kind=='trial' and (c.trial_used or active(c)):return 'التجربة غير متاحة لهذا الحساب.'
            # Subscription approval is a manual administrative attestation of external payment.
            bal=add_credits(db,req.telegram_id,days,f'request:{rid}',kind='purchase')
            req.status='approved'
            queued_history=queue_recent_closed(db,req.telegram_id,int(os.getenv('HISTORY_ON_ACTIVATION_COUNT','5')))
            notice=(f'✅ تمت إضافة {_days(days)} إلى رصيدك في الصفوة. رصيدك الآن: {_days(bal)}.\n'
                    'ستصلك التوصيات الجديدة هنا فور صدورها. أهلًا بك 👑')
            admin_msg=f'✅ تمت إضافة {_days(days)} للعميل {req.telegram_id}. رصيده الآن: {_days(bal)}.'
        else:
            req.status='rejected';notice='تمت مراجعة طلب التفعيل، ولم تتم الموافقة عليه.'
            admin_msg='❌ تم رفض الطلب.'
        req.decided_at=utcnow();target=req.telegram_id
    if action=='approve':set_expired_commands(target,False)
    try:send(target,notice,MAIN_MENU if action=='approve' else None)
    except Exception:LOG.exception('Notification failed')
    return admin_msg

def interact(uid,username,data):
    ensure_user(uid,username)
    if data.startswith('approve:') or data.startswith('reject:'):
        action,rid=data.split(':',1)
        if not rid.isdigit():return 'طلب غير صحيح.'
        return decide(uid,action,int(rid))
    if data.startswith('grant:'):
        _,rid,days=(data.split(':')+['',''])[:3]
        if not (rid.isdigit() and days.isdigit()):return 'طلب غير صحيح.'
        return decide(uid,'approve',int(rid),int(days))
    if data=='noop':return 'تم الإلغاء، ولم يُفعَّل أي شيء.'
    if data=='request:trial':return submit_request(uid,'trial')
    if data=='request:subscription':return submit_request(uid,'subscription')
    if data=='mine':return mine(uid)
    if data=='perf':return 'تم إلغاء هذه الخدمة.'
    if data=='account':return account(uid)
    return 'اختر من القائمة.'

def process(update):
    msg=update.get('message')
    cb=update.get('callback_query')
    # العميل اللي تجربته أو اشتراكه خلص: كل حاجة بترد بزرار خدمة العملاء بس
    who_id=(cb or {}).get('from',{}).get('id') or ((msg or {}).get('from') or {}).get('id')
    if who_id and (cb or (msg and msg['chat']['type']=='private')):
        ensure_user(who_id,((cb or msg).get('from') or {}).get('username',''))
        state=expired_state(who_id)
        if state:
            if cb:call('answerCallbackQuery',{'callback_query_id':cb['id']})
            data=(cb or {}).get('data','')
            text=((msg or {}).get('text') or '').strip()
            if data=='buy' or text.split('@')[0].lower()=='/subscribe' and WHOP_API_KEY:
                send_buy(who_id)
            elif data=='request:subscription' and not WHOP_CHECKOUT_URL:
                send(who_id,subscribe_now(who_id),None)
            elif text and not text.startswith('/'):
                w,_=_who(who_id)
                _notify_admins(f'✉️ رسالة من عميل (اشتراكه منتهي)\nالعميل: {w}\nالـ ID: {who_id}\n\n{text[:1500]}\n\nللرد: /r {who_id} الرسالة')
                send(who_id,ended_short(state)+'\nاضغط «اشترك الآن» لإتمام الاشتراك.',subscribe_kb())
            else:
                send(who_id,ended_short(state),subscribe_kb())
            return
    if cb and cb.get('data')=='buy':
        call('answerCallbackQuery',{'callback_query_id':cb['id']})
        send_buy(cb['from']['id']);return
    if cb and cb.get('data') in ('pre','contact'):
        uid=cb['from']['id'];ensure_user(uid,cb['from'].get('username',''))
        call('answerCallbackQuery',{'callback_query_id':cb['id']})
        (send_premarket if cb['data']=='pre' else contact)(uid)
        return
    if cb:
        uid=cb['from']['id'];username=cb['from'].get('username','')
        response=interact(uid,username,cb.get('data',''))
        kb=MAIN_MENU
        if isinstance(response,tuple):response,kb=response
        call('answerCallbackQuery',{'callback_query_id':cb['id'],'text':response[:190],'show_alert':False})
        send(uid,response,kb)
        return
    if msg and msg['chat']['type']=='private':
        uid=msg['from']['id'];ensure_user(uid,msg['from'].get('username',''))
        text=(msg.get('text') or '').strip()
        cmd=text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
        # أوامر القائمة
        if cmd=='/subscribe' and WHOP_API_KEY:
            send_buy(uid);return
        if cmd=='/subscribe' and WHOP_CHECKOUT_URL:
            send(uid,'💳 اضغط الزر التالي لإتمام الاشتراك والدفع بأمان عبر Whop.',subscribe_kb());return
        if cmd in ('/trial','/subscribe','/mine','/account'):
            r={'/trial':lambda:auto_trial(uid),'/subscribe':lambda:subscribe_now(uid),
               '/mine':lambda:mine(uid),'/account':lambda:account(uid)}[cmd]()
            send(uid,r,MAIN_MENU);return
        if cmd=='/premarket':send_premarket(uid);return
        if cmd=='/contact':contact(uid);return
        # الإدارة: /sub <رقم الطلب> <عدد الأيام> ← تفعيل اشتراك بعدد أيام مخصص
        if uid in ADMINS and text.startswith('/sub'):
            p=text.split()
            if len(p)!=3 or not p[1].isdigit() or not p[2].isdigit() or int(p[2])<1:
                send(uid,'الصيغة: /sub <رقم الطلب> <عدد التوصيات>\nمثال: /sub 12 40');return
            send(uid,decide(uid,'approve',int(p[1]),int(p[2])) );return
        # الإدارة: /add <ID> <عدد> ← إضافة توصيات للرصيد ، /set <ID> <عدد> ← تحديد الرصيد (0 = إيقاف)
        if uid in ADMINS and (text.startswith('/add') or text.startswith('/set')):
            p=text.split()
            if len(p)!=3 or not p[1].isdigit() or not p[2].isdigit():
                send(uid,'الصيغة:\n/add <ID العميل> <عدد التوصيات> ← إضافة للرصيد\n/set <ID العميل> <عدد التوصيات> ← تحديد الرصيد (0 = إيقاف)');return
            tid,n=int(p[1]),int(p[2])
            with Session.begin() as db:
                c=db.get(Customer,tid)
                if not c:send(uid,'هذا العميل غير موجود في البوت.');return
                cr=credit(db,tid)
                if text.startswith('/add'):
                    bal=add_credits(db,tid,n,f'admin:{utcnow().isoformat()}',kind='admin')
                else:
                    cr.balance=n;cr.mode='paid' if n else cr.mode
                    if not n and cr.mode=='trial':cr.trial_wins=TRIAL_WINS
                    db.add(credit(db,tid));bal=n
                ok=eligible(db,tid)
                c.status='active' if ok else 'expired'
            set_expired_commands(tid,not ok)
            if ok:
                try:send(tid,f'✅ رصيدك في الصفوة الآن: {_days(bal)}.',MAIN_MENU)
                except Exception:pass
            send(uid,f'✅ رصيد العميل {tid} الآن: {_days(bal)}.' if ok else f'⛔ تم إيقاف اشتراك العميل {tid}.')
            return
        # الإدارة: /customers ← قايمة كل العملا (رسالة + ملف CSV)
        if uid in ADMINS and cmd=='/customers':
            send_customers(uid);return
        if text=='/myid':
            send(uid,f'رقم حسابك (ID): {uid}');return
        # الإدارة: /r <ID> <الرسالة> ← رد على عميل من البوت
        if uid in ADMINS and text.startswith('/r '):
            parts=text.split(' ',2)
            if len(parts)<3 or not parts[1].isdigit():
                send(uid,'الصيغة: /r <ID العميل> <الرسالة>');return
            try:send(int(parts[1]),f'📩 رسالة من فريق الصفوة:\n{parts[2]}');send(uid,'✅ تم الإرسال')
            except Exception as exc:send(uid,f'لم يتم الإرسال: {str(exc)[:150]}')
            return
        # أي رسالة من عميل (غير /start) بتتحول للإدارة، زي رقم الواتساب
        if text and not text.startswith('/') and uid not in ADMINS:
            who,_=_who(uid)
            _notify_admins(f'✉️ رسالة من عميل\nالعميل: {who}\nالـ ID: {uid}\n\n{text[:1500]}\n\nللرد: /r {uid} الرسالة')
            send(uid,'✅ وصلت رسالتك إلى فريق الصفوة، وسنرد عليك في أقرب وقت.',MAIN_MENU)
            return
        send(uid,f'أهلًا بك في الصفوة 👑\nقم بتفعيل التجربة المجانية حتى تحقيق {TRIAL_WINS} توصيات ناجحة، وبعدها يمكنك الاشتراك بباقات التوصيات.',MAIN_MENU)

def deliver_historical():
    """Send queued onboarding history to active accounts, with no new-trade implication."""
    with Session() as db:
        ids=list(db.scalars(select(HistoricalDelivery.id).where(
            HistoricalDelivery.status=='queued').order_by(HistoricalDelivery.id).limit(25)).all())
    for hid in ids:
        with Session() as db:
            h=db.get(HistoricalDelivery,hid)
            c=db.get(Customer,h.telegram_id)
            if not active(c):
                continue  # Keep queued until account is active.
            target=h.telegram_id
            body=h.payload
        try:
            send(target,body)
            with Session.begin() as db:
                row=db.get(HistoricalDelivery,hid)
                row.status='sent'
                row.sent_at=utcnow()
            time.sleep(.07)
        except Exception as exc:
            LOG.warning('historical deliver %s failed: %s',hid,str(exc)[:100])
            break

def deliver():
    with Session() as db:
        ids=list(db.scalars(select(Delivery.id).where(Delivery.status=='queued').order_by(Delivery.id).limit(35)).all())
    for did in ids:
        with Session() as db:
            d=db.get(Delivery,did);ev=db.get(Event,d.event_id);c=db.get(Customer,d.telegram_id)
            if not c:
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
    try:
        call('setMyCommands',{'commands':BOT_COMMANDS})
        call('setChatMenuButton',{'menu_button':{'type':'commands'}})
        call('setMyDescription',{'description':BOT_DESCRIPTION})
        call('setMyShortDescription',{'short_description':BOT_SHORT_DESCRIPTION})
    except Exception:LOG.exception('Cannot set bot commands')
    offset=None
    last_check=0
    while True:
        try:
            if time.time()-last_check>=60:
                last_check=time.time()
                try:check_expiries()
                except Exception:LOG.exception('expiry check failed')
            deliver_historical()
            deliver()
            updates=call('getUpdates',{'offset':offset,'timeout':10,'allowed_updates':['message','callback_query']},timeout=20)
            for u in updates:
                offset=u['update_id']+1
                try:process(u)
                except Exception:LOG.exception('Failed to process update')
        except Exception:
            LOG.exception('Polling error');time.sleep(3)
if __name__=='__main__': main()
