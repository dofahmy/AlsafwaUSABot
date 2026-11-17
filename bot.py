"""Telegram long polling bot. No purchases, checkout links, or payment verification via chat."""
import os, time, json, logging
from datetime import datetime, timezone
from sqlalchemy import select
from common import Session, Customer, Request, Event, Delivery, HistoricalDelivery, queue_recent_closed, grant, active, utcnow
from telegram_api import call,send
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
MAIN_MENU=[[{'text':'🎁 تجربة مجانية (جلستين تداول)','callback_data':'request:trial'}, {'text':'💳 الاشتراك الآن','callback_data':'request:subscription'}],
           [{'text':'📈 توصياتي','callback_data':'mine'}, {'text':'👤 حسابي','callback_data':'account'}],
           [{'text':'📊 أداء الشهر الماضي','callback_data':'perf'}]]

def ensure_user(tid,username=''):
    with Session.begin() as db:
        c=db.get(Customer,tid)
        if not c: db.add(Customer(telegram_id=tid,username=username))
        elif username and c.username!=username:c.username=username

def account(tid):
    with Session() as db:
        c=db.get(Customer,tid)
        if not c or not active(c): return 'حالة الاشتراك: غير نشط. يمكنك بدء التجربة المجانية أو الاشتراك من القائمة.'
        return f'✅ اشتراكك نشط حتى {c.access_until:%Y-%m-%d %H:%M} UTC.'

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


def _count(n,one,many):
    if n==1:return f'{one} واحدة'
    if n==2:return {'توصية':'توصيتان','جلسة':'جلستان'}.get(one,f'2 {one}')
    return f'{n} {many if 3<=n<=10 else one}'


def _fmt_day(d):
    return d.strftime('%d/%m')


def performance(days=30):
    """أداء التوصيات آخر 30 يومًا من قاعدة بيانات البوت (متاح لأي حد يفتح البوت)."""
    from collections import defaultdict
    today=utcnow().astimezone(NY).date()
    start=today-timedelta(days=days)
    with Session() as db:
        evs=db.scalars(select(Event).order_by(Event.id)).all()
    sig=defaultdict(list)
    for e in evs:
        try:d=datetime.strptime(e.signal_id.split(':')[0],'%Y-%m-%d').date()
        except Exception:continue
        if start<=d<=today:sig[e.signal_id].append(e)
    rows=[]
    for sid,es in sig.items():
        kinds={e.kind for e in es}
        opened=next((e for e in es if e.kind=='open'),None)
        if not opened:continue
        d=datetime.strptime(sid.split(':')[0],'%Y-%m-%d').date()
        if 'cancelled' in kinds:res='cancelled'
        elif 'target2' in kinds:res='target2'
        elif 'target1' in kinds:res='target1'
        elif 'stop' in kinds:res='stop'
        elif 'eod' in kinds:res='eod'
        elif '⏳' in (opened.payload or '') and 'activated' not in kinds:res='pending'
        else:res='open'
        last=[e for e in es if e.kind in ('target1','target2','stop','eod')]
        pct=_pct(last[-1].payload) if last else None
        rows.append({'d':d,'sym':es[0].symbol,'res':res,'pct':pct})
    if not rows:
        return 'لا توجد توصيات خلال آخر 30 يومًا بعد.'
    act=[r for r in rows if r['res'] not in ('cancelled','pending')]
    canc=sum(r['res']=='cancelled' for r in rows)
    wins=[r for r in act if r['res'] in ('target1','target2')]
    stops=[r for r in act if r['res']=='stop']
    dec=len(wins)+len(stops)
    rets=[r for r in act if r['pct'] is not None]
    sessions=len({r['d'] for r in rows})
    out=[f'📊 أداء توصيات الصفوة — آخر {days} يومًا',
         f'من {start:%d/%m/%Y} إلى {today:%d/%m/%Y}','',
         '📌 ملخص',
         f"• عدد التوصيات: {_count(len(rows),'توصية','توصيات')} خلال {_count(sessions,'جلسة','جلسات')} تداول",
         f'• تم تفعيلها: {len(act)} — ملغية لعدم التفعيل: {canc}']
    if dec:out.append(f'• نسبة النجاح: {len(wins)/dec:.0%} ({len(wins)} من {dec})')
    out+=['','🎯 النتائج',
          f"• حققت الهدف الثاني: {sum(r['res']=='target2' for r in act)}",
          f"• حققت الهدف الأول: {sum(r['res']=='target1' for r in act)}",
          f'• ضربت وقف الخسارة: {len(stops)}',
          f"• أُغلقت نهاية اليوم دون هدف أو وقف: {sum(r['res']=='eod' for r in act)}"]
    still=sum(r['res'] in ('open','pending') for r in rows)
    if still:out.append(f'• ما زالت مفتوحة: {still}')
    if rets:
        best=max(rets,key=lambda r:r['pct']);worst=min(rets,key=lambda r:r['pct'])
        out+=['','💵 العائد',
              f"• متوسط عائد التوصية: {sum(r['pct'] for r in rets)/len(rets):+.1f}%",
              f"• أفضل توصية: {best['sym']} \u200e{best['pct']:+.1f}%"]
        if worst['pct']<0:out.append(f"• أكبر خسارة: {worst['sym']} \u200e{worst['pct']:+.1f}%")
    weeks=[]
    ws=start
    while ws<=today:
        we=min(ws+timedelta(days=6),today)
        wr=[r for r in act if ws<=r['d']<=we]
        if wr:
            w_w=sum(r['res'] in ('target1','target2') for r in wr);w_l=sum(r['res']=='stop' for r in wr)
            line=f"• {_fmt_day(ws)} – {_fmt_day(we)}: {_count(len(wr),'توصية','توصيات')}"
            if w_w+w_l:line+=f' — نجاح {w_w/(w_w+w_l):.0%}'
            weeks.append(line)
        ws=we+timedelta(days=1)
    if weeks:out+=['','📅 الأسابيع']+weeks
    byday=defaultdict(list)
    for r in act:byday[r['d']].append(r)
    cand=[(sum(x['res'] in ('target1','target2') for x in v),sum(x['res'] in ('target1','target2','stop') for x in v),d)
          for d,v in byday.items()]
    cand=[c for c in cand if c[1]]
    if cand:
        w_,n_,d_=max(cand,key=lambda c:(c[0],c[0]/c[1]))
        out+=['',f'🏆 أفضل يوم: {_fmt_day(d_)} — {w_} من {n_} حققت الهدف']
    out+=['','⚠️ الأداء السابق لا يضمن النتائج المستقبلية.']
    return '\n'.join(out)


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
            pct=_pct(last.payload) if last.kind!='open' else None
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
        if active(c):
            return f'✅ اشتراكك نشط بالفعل حتى {c.access_until:%Y-%m-%d}.'
        if c.trial_used:
            return 'لقد استخدمت التجربة المجانية من قبل 🙏\nإذا رغبت في الاستمرار معنا اضغط «💳 الاشتراك الآن».'
        until=trial_until()
        c.access_until=until;c.status='active';c.trial_used=True;db.add(c)
        req=Request(telegram_id=tid,kind='trial',status='approved');req.decided_at=utcnow()
        db.add(req)
        queue_recent_closed(db,tid,int(os.getenv('HISTORY_ON_ACTIVATION_COUNT','5')))
    who,_=_who(tid)
    _notify_admins(f'🎁 تم تفعيل تجربة مجانية تلقائيًا\nالعميل: {who}\nالـ ID: {tid}\nحتى: {until:%Y-%m-%d %H:%M} UTC')
    end_ny=until.astimezone(NY)
    return (f'🎉 تم تفعيل التجربة المجانية لمدة جلستين تداول، وتنتهي يوم {end_ny:%d/%m} الساعة 8 مساءً بتوقيت نيويورك.\n'
            'ستصلك التوصيات الجديدة هنا فور صدورها.\n'
            'وبعد انتهاء التجربة يمكنك التواصل معنا من «💳 الاشتراك الآن» إذا رغبت في استمرار الخدمة.')


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


DAY_CHOICES=[7,14,30,60,90,180,365]


def _days(n):
    return f'{n} {"أيام" if 3<=n<=10 else "يوم"}'


def ask_days(actor,rid):
    """لما الإدارة تدوس «تفعيل الاشتراك»: البوت بيسأل المدة الأول، وما بيفعّلش غير بعد اختيارها."""
    if actor not in ADMINS:return 'غير مصرح'
    with Session() as db:
        req=db.get(Request,rid)
        if not req or req.status!='pending':return 'الطلب تمت معالجته أو غير موجود.'
        tid=req.telegram_id
        cur=db.get(Customer,tid)
        cur_line=(f'\nاشتراكه الحالي ساري حتى {cur.access_until.astimezone(NY):%d/%m/%Y}، وستبدأ المدة الجديدة من اليوم.'
                  if active(cur) else '')
    who,_=_who(tid)
    rows=[]
    for i in range(0,len(DAY_CHOICES),4):
        rows.append([{'text':_days(d),'callback_data':f'grant:{rid}:{d}'} for d in DAY_CHOICES[i:i+4]])
    rows.append([{'text':'❌ إلغاء','callback_data':'noop'}])
    return (f'⏳ تفعيل اشتراك الطلب #{rid}\nالعميل: {who} (ID: {tid}){cur_line}\n\nاختر مدة الاشتراك (تبدأ من اليوم).\n'
            f'أو لعدد أيام آخر أرسل: /sub {rid} عدد_الأيام\nمثال: /sub {rid} 45'),rows


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
            # المدة بتبدأ من النهارده (مش بتتضاف على تجربة أو اشتراك قديم)
            c.access_until=utcnow()+timedelta(days=days);c.status='active';db.add(c)
            until=c.access_until
            req.status='approved'
            queued_history=queue_recent_closed(db,req.telegram_id,int(os.getenv('HISTORY_ON_ACTIVATION_COUNT','5')))
            end_ny=until.astimezone(NY)
            notice=(f'✅ تم تفعيل اشتراكك في الصفوة لمدة {_days(days)}، وينتهي يوم {end_ny:%d/%m/%Y}.\n'
                    'ستصلك التوصيات الجديدة هنا فور صدورها. أهلًا بك 👑')
            admin_msg=f'✅ تم تفعيل اشتراك {_days(days)} للعميل {req.telegram_id} حتى {end_ny:%d/%m/%Y}.'
        else:
            req.status='rejected';notice='تمت مراجعة طلب التفعيل، ولم تتم الموافقة عليه.'
            admin_msg='❌ تم رفض الطلب.'
        req.decided_at=utcnow();target=req.telegram_id
    try:send(target,notice)
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
    if data=='perf':return performance()
    if data=='account':return account(uid)
    return 'اختر من القائمة.'

def process(update):
    msg=update.get('message')
    cb=update.get('callback_query')
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
        # الإدارة: /sub <رقم الطلب> <عدد الأيام> ← تفعيل اشتراك بعدد أيام مخصص
        if uid in ADMINS and text.startswith('/sub'):
            p=text.split()
            if len(p)!=3 or not p[1].isdigit() or not p[2].isdigit() or int(p[2])<1:
                send(uid,'الصيغة: /sub <رقم الطلب> <عدد الأيام>\nمثال: /sub 12 45');return
            send(uid,decide(uid,'approve',int(p[1]),int(p[2])) );return
        # الإدارة: /set <ID العميل> <عدد الأيام> ← تظبيط اشتراك عميل من النهارده (0 = إيقاف)
        if uid in ADMINS and text.startswith('/set'):
            p=text.split()
            if len(p)!=3 or not p[1].isdigit() or not p[2].isdigit():
                send(uid,'الصيغة: /set <ID العميل> <عدد الأيام>\nمثال: /set 123456789 7\n(0 = إيقاف الاشتراك)');return
            n=int(p[2])
            with Session.begin() as db:
                c=db.get(Customer,int(p[1]))
                if not c:send(uid,'هذا العميل غير موجود في البوت.');return
                c.access_until=utcnow()+timedelta(days=n) if n else utcnow()-timedelta(seconds=1)
                c.status='active' if n else 'inactive';db.add(c);until=c.access_until
            send(uid,f'✅ أصبح اشتراك العميل {p[1]} ساريًا حتى {until.astimezone(NY):%d/%m/%Y}.' if n else f'⛔ تم إيقاف اشتراك العميل {p[1]}.')
            return
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
        send(uid,'أهلًا بك في الصفوة 👑\nيمكنك الاستمتاع بتجربة مجانية لمدة جلستين تداول، وبعدها التواصل معنا في حال الرغبة في استمرار الخدمة.',MAIN_MENU)

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
