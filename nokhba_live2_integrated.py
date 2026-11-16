# -*- coding: utf-8 -*-
"""
الماسح الحي (تجريبي) — بوت راجح للسوق الأمريكي — بنفس قواعد الاختبار الأخير:
  القواعد السبعة + الطريقتين (أ أو ب) + الشروط الستة + البلاد المسموحة،
  والأهداف زي النخبة (الأول = الإشارة × ١٫١٢، التاني = × ١٫٢٥ مقرّب لتحت بسنت)، والوقف ١٢٪.

بيدوّر عند كل قفلة :15 و:45 بتوقيت نيويورك (من 7:15 الصبح لحد 3:45 العصر)،
وبيبعت الإشارة على القناة بعد القفلة بدقيقتين (:17 و:47)، ويتابعها:
  الهدف الأول ← رسالة فيها الاختيارين، الهدف التاني ← رسالة، الوقف قبل الهدف الأول ← رسالة.
  اللي ما خلصتش في اليوم بتتكتب «مفتوحة» ومش بتتحسب في الحصاد (زي النخبة).
وفي آخر اليوم بيبعت «حصاد اليوم».

البيانات من ألباكا (المفاتيح في alpaca_keys.txt):
  ألباكا المجانية ما بتديش آخر ١٥ دقيقة من البيانات الكاملة (SIP)،
  فبناخد البيانات الكاملة لحد قبلها بـ١٦ دقيقة، والباقي من بورصة IEX (لحظية)،
  وبنكبّر حجم IEX بنسبته للحجم الكامل في الساعة اللي قبلها.

الملفات اللي لازم تكون في نفس الفولدر:
  nokhba_vol.py  nokhba_rules.py  nokhba_apply.py  alpaca_keys.txt  cache_info.json (لو موجود)
  telegram_token.txt  ← سطر واحد فيه توكن البوت (ما تبعتيهوش في الشات)

التشغيل:
  py nokhba_live2.py --test              رسالة تجربة على القناة
  py nokhba_live2.py --dry               يشتغل بس يطبع الرسايل بدل ما يبعتها
  py nokhba_live2.py --replay 2026-11-12 يعيد يوم قديم كأنه حي (يطبع بس) علشان نتأكد إنه زي الاختبار
  py nokhba_live2.py --replay 2026-11-12 --send            يطبعها ويبعتها كمان على القناة
  py nokhba_live2.py --replay 2026-11-12 --send --chat @x  يبعتها لقناة/شات تاني بدل قناة العملا
  py nokhba_live2.py                     التشغيل الحقيقي (سيبي النافذة مفتوحة)

الملفات اللي بتطلع:
  live2_signals.csv  كل إشارة ونتيجتها
  live2_state.json   حالة اليوم (علشان لو قفلتي وفتحتي يكمّل)
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import timedelta

import numpy as np
import pandas as pd
import requests

import nokhba_vol as V
import nokhba_rules as R
import nokhba_apply as A
from bridge import emit  # optional separate customer bot integration

NY, CAIRO = V.NY, V.CAIRO

# ================= الإعدادات =================
CHAT_ID = "@microstockss"
CHANNEL_TITLE = "بوت راجح للسوق الأمريكي"
TOKEN_FILE = "telegram_token.txt"
STATE_FILE = "live2_state.json"
LOG_FILE = "live2_signals.csv"
STOP = 0.12
SCAN_FIRST, SCAN_LAST = "07:15", "15:45"
WATCH_FROM, WATCH_TO = "04:00", "15:45"     # تجميع الأسهم الطايرة كل ٥ دقايق
TRACK_TO = "20:00"                          # متابعة الصفقات لحد ٨ بالليل نيويورك (بعد الإغلاق كمان)
CLOSE_AT = "15:55"                          # أي صفقة لسه مفتوحة نقفلها ٣:٥٥ العصر نيويورك (١٠:٥٥ بالليل القاهرة)
EOD = "اتقفلت آخر اليوم"
SPY_UP = 0.25   # إس بي واي عن قفلة امبارح: طالع ٠٫٢٥٪+ ← مرتفع، نازل ٠٫٢٥٪+ ← متوسط، غير كده ← منخفض
SUMMARY_AT = "20:05"                        # حصاد اليوم (٣:٠٥ الفجر بتوقيت القاهرة)
MOVER_MIN = 0.18                            # السهم يدخل قايمة المتابعة لو طلع ١٨٪ أو أكتر
SIP_DELAY_MIN = 16
DRY_RUN = False
SEND_DELAY = 0                              # ثواني بين الرسايل (في الإعادة علشان تيليجرام ما يرفضش)
# ============================================

ORIG_GET_BARS = V.get_bars
WIN = {"الهدف الأول", "الهدف الثاني"}


def token():
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE, encoding="utf-8-sig").read().strip()
    return os.environ.get("TELEGRAM_TOKEN", "")


def tg_send(text, reply_to=None):
    tok = token()
    print("\n[تيليجرام]\n" + text + "\n")
    if DRY_RUN or not tok:
        if not DRY_RUN:
            print("(ملف telegram_token.txt مش موجود، فالرسالة اتطبعت بس)")
        return None
    if SEND_DELAY:
        time.sleep(SEND_DELAY)
    data = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    if reply_to:
        data["reply_to_message_id"] = reply_to
        data["allow_sending_without_reply"] = True
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=20).json()
            if r.get("ok"):
                return r["result"]["message_id"]
            print("خطأ تيليجرام:", r)
        except Exception as e:
            print("خطأ اتصال بتيليجرام:", e)
        time.sleep(3)
    return None


def send_update(tr, kind, message, reply_to=None):
    # Keep existing channel delivery independent of customer API.
    result = tg_send(message, reply_to=reply_to)
    emit(tr, kind, message)
    return result


def fmt(p):
    return f"{p:.2f}" if p >= 1 else (f"{p:.3f}" if p >= 0.1 else f"{p:.4f}")


# ---------------- بيانات لحظية ----------------
def fetch(sym, start, end, feed):
    rows, tok = [], None
    for _ in range(20):
        p = {"timeframe": "1Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "adjustment": "raw", "feed": feed}
        if tok:
            p["page_token"] = tok
        r = requests.get(f"{V.BASE}/{sym}/bars", headers=V.HEAD, params=p, timeout=30)
        if r.status_code == 429:
            time.sleep(5)
            continue
        if r.status_code != 200:
            print(f"  {sym} {feed}: {r.status_code} {r.text[:100]}")
            break
        j = r.json()
        rows += j.get("bars") or []
        tok = j.get("next_page_token")
        time.sleep(0.32)
        if not tok:
            break
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(NY)
    return df.set_index("t").rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})


def live_minutes(sym, day):
    """شموع النهارده: الكاملة لحد قبل ١٦ دقيقة، والباقي من IEX بحجم متكبّر."""
    now = pd.Timestamp.now("UTC")
    start = pd.Timestamp(f"{day} 04:00", tz=NY).tz_convert("UTC")
    cut = now - timedelta(minutes=SIP_DELAY_MIN)
    sip = fetch(sym, start, cut, "sip") if cut > start else pd.DataFrame()
    iex = fetch(sym, max(start, cut - timedelta(minutes=60)), now, "iex")
    if iex.empty:
        return sip
    cut_ny = cut.tz_convert(NY)
    k = 1.0
    if not sip.empty:
        a = sip[sip.index >= cut_ny - timedelta(minutes=60)]["Volume"].sum()
        b = iex[iex.index < cut_ny]["Volume"].sum()
        if b > 0 and a > 0:
            k = max(a / b, 1.0)
    recent = iex[iex.index >= cut_ny].copy()
    recent["Volume"] = recent["Volume"] * k
    return pd.concat([sip[sip.index < cut_ny] if not sip.empty else sip, recent]).sort_index()


def live_get_bars(sym, timeframe, start, end, adjustment="raw"):
    """بدل V.get_bars جوّه الماسح الحي: شموع النهارده لحظية، والباقي من الكاش."""
    today = pd.Timestamp.now(tz=NY).strftime("%Y-%m-%d")
    if timeframe == "1Min" and start[:10] == today:
        return live_minutes(sym, today)
    return ORIG_GET_BARS(sym, timeframe, start, end, adjustment)


# ---------------- الأسهم الطايرة ----------------
def price_universe(day):
    """كل الأسهم اللي إغلاقها امبارح في حدود القاعدة (٠٫٤٧–٣٫٠١)."""
    syms = R.all_symbols()
    d1 = pd.Timestamp(day) - timedelta(days=1)
    d0 = d1 - timedelta(days=7)
    lo, hi = next((l, h) for f, l, h in A.RULES if f == "إغلاق أمس")
    out = {}
    for i in range(0, len(syms), 200):
        chunk = syms[i:i + 200]
        p = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": f"{d0:%Y-%m-%d}T00:00:00Z",
             "end": f"{d1:%Y-%m-%d}T23:59:00Z", "limit": 10000, "adjustment": "raw", "feed": "sip"}
        tok = None
        while True:
            if tok:
                p["page_token"] = tok
            r = requests.get(f"{V.BASE}/bars", headers=V.HEAD, params=p, timeout=60)
            if r.status_code == 429:
                time.sleep(5)
                continue
            if r.status_code != 200:
                break
            j = r.json()
            for s, b in (j.get("bars") or {}).items():
                if b:
                    c = b[-1]["c"]
                    if lo * 0.9 <= c <= hi * 1.1:
                        out[s] = c
            tok = j.get("next_page_token")
            time.sleep(0.32)
            if not tok:
                break
    return out


def movers_now(universe):
    """الأسهم اللي سعرها دلوقتي (IEX) طالع ١٨٪ أو أكتر عن إغلاق أمس."""
    syms, hits = list(universe), set()
    for i in range(0, len(syms), 200):
        chunk = syms[i:i + 200]
        try:
            r = requests.get(f"{V.BASE}/snapshots", headers=V.HEAD,
                             params={"symbols": ",".join(chunk), "feed": "iex"}, timeout=30)
            if r.status_code != 200:
                continue
            for s, x in r.json().items():
                pc = universe.get(s)
                if not pc or not x:
                    continue
                prices = [((x.get("latestTrade") or {}).get("p")), ((x.get("minuteBar") or {}).get("h")),
                          ((x.get("dailyBar") or {}).get("h"))]
                prices = [p for p in prices if p]
                if prices and max(prices) >= pc * (1 + MOVER_MIN):
                    hits.add(s)
        except Exception as e:
            print("  خطأ في الأسهم الطايرة:", e)
        time.sleep(0.32)
    # كمان قايمة الأكتر صعودًا من ألباكا (لو متاحة)
    try:
        r = requests.get("https://data.alpaca.markets/v1beta1/screener/stocks/movers",
                         headers=V.HEAD, params={"top": 50}, timeout=30)
        if r.status_code == 200:
            for g in r.json().get("gainers", []):
                if g.get("symbol") in universe and g.get("percent_change", 0) >= MOVER_MIN * 100:
                    hits.add(g["symbol"])
    except Exception:
        pass
    return hits


# ---------------- الإشارة ----------------
def evaluate(sym, day, scan_label):
    """صورة السهم عند القفلة دي: بيرجّع (الصورة، الطريقة) لو عدّى على كل حاجة، أو None."""
    rows = R.snapshots(sym, day)
    if not rows:
        return None
    s = pd.DataFrame(rows)
    s["chg30"] = (s["السعر"] / s["السعر"].shift(1) - 1).fillna(0) * 100
    s = A.add_minute(s)
    s = s[s["الوقت نيويورك"] <= scan_label]
    if s.empty or s.iloc[-1]["الوقت نيويورك"] != scan_label:
        return None
    m = R.passes(s, A.RULES)
    for ex in A.EXCLUDES:
        m = m & ~R.passes(s, ex)
    ma, mb = m & R.passes(s, A.SETUPS["أ"]), m & R.passes(s, A.SETUPS["ب"])
    if not (ma[-1] or mb[-1]):
        return None
    setup = "أ+ب" if (ma[-1] and mb[-1]) else ("أ" if ma[-1] else "ب")
    return s.iloc[-1], setup


# ---------------- الحالة والسجل ----------------
def load_state(day):
    if os.path.exists(STATE_FILE):
        st = json.load(open(STATE_FILE, encoding="utf-8"))
        if st.get("day") == day:
            return st
        # يوم جديد: نسيب الصفقات المفتوحة من امبارح من غير متابعة (النخبة بتحسب نفس اليوم بس)
    return {"day": day, "watch": [], "signaled": [], "open": [], "done": [], "summary_sent": False,
            "last_scan": "", "universe": {}}


def save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def log_row(tr):
    cols = ["date", "ticker", "time_ny", "time_cairo", "setup", "entry", "t1", "t2", "stop", "result",
            "t1_time", "t2_time", "stop_time", "after_t1", "exit_price", "exit_pct", "spy_pct", "risk"]
    new = not os.path.exists(LOG_FILE)
    if not new:
        old = pd.read_csv(LOG_FILE, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        if list(old.columns) != cols:          # ملف من نسخة قديمة: نزوّد العمودين الجداد
            old.reindex(columns=cols).to_csv(LOG_FILE, index=False, encoding="utf-8-sig")
    with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(tr)


def spy_change(st, scan_ts):
    """إس بي واي طالع كام ٪ عن قفلة امبارح وقت التوصية.
    بعد ٩:٣٠: فجوة الفتح (سعر ٩:٣٠). قبلها: آخر سعر قبل التوصية."""
    day = st["day"]
    try:
        if not st.get("spy_prev"):
            d0 = pd.Timestamp(day)
            dd = ORIG_GET_BARS("SPY", "1Day", f"{d0 - timedelta(days=10):%Y-%m-%d}T00:00:00Z",
                               f"{d0 - timedelta(days=1):%Y-%m-%d}T23:59:00Z")
            st["spy_prev"] = float(dd["Close"].iloc[-1])
        d0 = pd.Timestamp(day)
        m = V.get_bars("SPY", "1Min", f"{d0:%Y-%m-%d}T08:00:00Z", f"{d0 + timedelta(days=1):%Y-%m-%d}T01:00:00Z")
        m = m[(m.index.date == d0.date()) & (m.index < scan_ts)]
        if m.empty:
            return None
        reg = m[m.index.strftime("%H:%M") >= "09:30"]
        px = float(reg["Open"].iloc[0]) if len(reg) else float(m["Close"].iloc[-1])
        return (px / st["spy_prev"] - 1) * 100
    except Exception as e:
        print("  ما قدرتش أجيب إس بي واي:", e)
        return None


def open_signal(st, sym, row, setup, scan_ts):
    e = float(row["السعر"])
    t1, t2 = A.nokhba_targets(e)
    tr = {"date": st["day"], "ticker": sym, "time_ny": scan_ts.strftime("%H:%M"),
          "time_cairo": (scan_ts + timedelta(minutes=1)).tz_convert(CAIRO).strftime("%H:%M"),
          "time_ny_msg": (scan_ts + timedelta(minutes=1)).tz_convert(NY).strftime("%H:%M"),
          "setup": setup, "entry": round(e, 4), "t1": float(t1), "t2": float(t2),
          "stop": round(e * (1 - STOP), 4), "since": scan_ts.isoformat(), "hit_t1": False,
          "result": "مفتوحة", "t1_time": "", "t2_time": "", "stop_time": "", "after_t1": ""}
    text = (f"✨ {CHANNEL_TITLE} | فرصة شراء\n\n"
            f"🔷 السهم: {sym}\n"
            f"💰 سعر الدخول: {fmt(e)}\n"
            f"🎯 الهدف الأول: {fmt(t1)}\n"
            f"🎯 الهدف الثاني: {fmt(t2)}\n"
            f"⛔ وقف الخسارة: {fmt(tr['stop'])}\n\n"
            f"📌 عند الهدف الأول:\n"
            f"1️⃣ بيع كل الكمية وجني الربح\n"
            f"أو 2️⃣ بيع 50% وحرّك الوقف لسعر الدخول وانتظر الهدف الثاني\n\n"
            f"🕒 {tr['time_ny_msg']} بتوقيت نيويورك")
    spy = spy_change(st, scan_ts)
    tr["spy_pct"] = round(spy, 2) if spy is not None else ""
    if spy is not None:
        level = "مرتفع" if spy >= SPY_UP else ("متوسط" if spy <= -SPY_UP else "منخفض")
        tr["risk"] = level
        text += f"\n\n⚠️ مستوى المخاطرة اليوم: {level}"
    tr["msg_id"] = tg_send(text)
    emit(tr, "open", text)
    st["open"].append(tr)
    st["signaled"].append(sym)
    print(f"  إشارة: {sym} {fmt(e)} ({setup})")


def update_open(st, bars_fn):
    still = []
    for tr in st["open"]:
        b = bars_fn(tr["ticker"], st["day"])
        if b is None or b.empty:
            still.append(tr)
            continue
        after = b[b.index >= pd.Timestamp(tr["since"])]
        finished = False
        for ts, bar in after.iterrows():
            if not tr["hit_t1"]:
                if bar["Low"] <= tr["stop"]:
                    tr["result"], tr["stop_time"] = "وقف خسارة", ts.strftime("%H:%M")
                    send_update(tr, "stop", f"{tr['ticker']}\n⛔ ضرب وقف الخسارة ({(tr['stop'] / tr['entry'] - 1) * 100:+.2f}%)",
                            reply_to=tr.get("msg_id"))
                    finished = True
                    break
                if bar["High"] >= tr["t1"]:
                    tr["hit_t1"], tr["t1_time"], tr["result"] = True, ts.strftime("%H:%M"), "الهدف الأول"
                    send_update(tr, "target1", f"{tr['ticker']}\n🎯 تحقق الهدف الأول (+{(tr['t1'] / tr['entry'] - 1) * 100:.2f}%)\n\n"
                            f"1️⃣ بعت الكمية كلها؟ الصفقة اتقفلت بربح ✅\n"
                            f"2️⃣ بعت 50%؟ حرّك الوقف لسعر الدخول {fmt(tr['entry'])} "
                            f"وانتظر الهدف الثاني {fmt(tr['t2'])}", reply_to=tr.get("msg_id"))
                    if bar["High"] >= tr["t2"]:
                        tr["t2_time"], tr["result"] = ts.strftime("%H:%M"), "الهدف الثاني"
                        send_update(tr, "target2", f"{tr['ticker']}\n🎯🎯 تحقق الهدف الثاني (+{(tr['t2'] / tr['entry'] - 1) * 100:.2f}%)",
                                reply_to=tr.get("msg_id"))
                        finished = True
                        break
                    continue
            else:
                if bar["High"] >= tr["t2"]:
                    tr["t2_time"], tr["result"] = ts.strftime("%H:%M"), "الهدف الثاني"
                    send_update(tr, "target2", f"{tr['ticker']}\n🎯🎯 تحقق الهدف الثاني (+{(tr['t2'] / tr['entry'] - 1) * 100:.2f}%)",
                            reply_to=tr.get("msg_id"))
                    finished = True
                    break
                if bar["Low"] <= tr["entry"]:
                    tr["after_t1"] = "رجع لسعر الدخول " + ts.strftime("%H:%M")   # داخلي، مش بيتنشر
                    finished = True
                    break
        if finished:
            st["done"].append(tr)
            log_row(tr)
            continue
        if len(after):
            tr["last"] = float(after["Close"].iloc[-1])
            tr["since"] = after.index[-1].isoformat()   # الشمعة الأخيرة ممكن لسه بتتكوّن
        still.append(tr)
    st["open"] = still


def close_eod(st):
    """الساعة ٣:٥٥ العصر: أي صفقة لسه مفتوحة نقفلها بآخر سعر (الاختبار قال الاستنا لتاني يوم أسوأ)."""
    for tr in st["open"]:
        last = tr.get("last")
        pct = f" ({(last / tr['entry'] - 1) * 100:+.2f}%)" if last else ""
        if not tr["hit_t1"]:
            tr["result"] = EOD
            if last:
                tr["exit_price"], tr["exit_pct"] = round(last, 4), round((last / tr["entry"] - 1) * 100, 2)
            send_update(tr, "eod", f"{tr['ticker']}\n🔔 اقفل الصفقة دلوقتي — اليوم قرب يخلص من غير هدف ولا وقف\n"
                    f"آخر سعر {fmt(last) if last else '—'}{pct}", reply_to=tr.get("msg_id"))
        else:
            send_update(tr, "eod", f"{tr['ticker']}\n🔔 لو لسه معاك الـ50% الباقية، اقفلها دلوقتي\n"
                    f"آخر سعر {fmt(last) if last else '—'}{pct}", reply_to=tr.get("msg_id"))
        st["done"].append(tr)
        log_row(tr)
    st["open"] = []
    st["eod_closed"] = True


def n_tawsia(n):
    return f"{n} توصيات" if 3 <= n <= 10 else f"{n} توصية"


def harvest(st):
    for tr in st["open"]:          # آخر اليوم: اللي لسه مفتوح يتسجّل كده
        if not tr["hit_t1"]:
            tr["result"] = "مفتوحة"
            last = tr.get("last")
            msg = f"{tr['ticker']}\n⏳ اليوم خلص من غير ما توصل للهدف الأول ولا الوقف"
            if last:
                msg += f"\nآخر سعر {fmt(last)} ({(last / tr['entry'] - 1) * 100:+.2f}%)"
            tg_send(msg, reply_to=tr.get("msg_id"))
        st["done"].append(tr)
        log_row(tr)
    st["open"] = []
    done = st["done"]
    lines = [f"📊 حصاد اليوم {st['day']} — {CHANNEL_TITLE}\n"]
    if not done:
        lines.append("مفيش توصيات النهارده.")
    w = l = 0
    rank = {"الهدف الثاني": 0, "الهدف الأول": 1, EOD: 2, "وقف خسارة": 3}
    for tr in sorted(done, key=lambda x: (rank.get(x["result"], 4), x["time_ny"])):   # الكسبانة الأول
        if tr["result"] == "الهدف الثاني":
            lines.append(f"✅ {tr['ticker']} — الهدف الثاني (+{(tr['t2'] / tr['entry'] - 1) * 100:.1f}%)")
            w += 1
        elif tr["result"] == "الهدف الأول":
            lines.append(f"✅ {tr['ticker']} — الهدف الأول (+{(tr['t1'] / tr['entry'] - 1) * 100:.1f}%)")
            w += 1
        elif tr["result"] == "وقف خسارة":
            lines.append(f"❌ {tr['ticker']} — وقف خسارة (−{STOP * 100:.0f}%)")
            l += 1
        elif tr["result"] == EOD:
            x = tr.get("exit_pct")
            lines.append(f"🔔 {tr['ticker']} — اتقفلت آخر اليوم من غير هدف ولا وقف"
                         + (f" ({x:+.1f}%)" if x is not None and x == x else ""))
    opens = [tr for tr in sorted(done, key=lambda x: x["time_ny"]) if tr["result"] == "مفتوحة"]
    if opens:
        if w + l:
            lines.append("")
        for tr in opens:
            last = tr.get("last")
            pct = f" (آخر سعر {(last / tr['entry'] - 1) * 100:+.1f}%)" if last else ""
            lines.append(f"⏳ {tr['ticker']} — ما وصلتش لهدف ولا وقف{pct}")
    if w + l:
        lines.append(f"\nنسبة النجاح النهارده: {w / (w + l):.0%} ({w} من {w + l})")
    if os.path.exists(LOG_FILE):
        allr = pd.read_csv(LOG_FILE, encoding="utf-8-sig").drop_duplicates(["date", "ticker"], keep="last")
        c = allr[~allr["result"].isin(["مفتوحة", EOD])]
        if len(c):
            lines.append(f"من البداية: {n_tawsia(len(c))}، نجاح {c['result'].isin(WIN).mean():.0%}")
    tg_send("\n".join(lines))


# ---------------- الحلقة ----------------
def scan(st, scan_ts, day, countries):
    label = scan_ts.strftime("%H:%M")
    cand = [s for s in st["watch"] if s not in st["signaled"] and countries.get(s) in A.COUNTRIES]
    print(f"{label} نيويورك: بيفحص {len(cand)} سهم")
    for sym in cand:
        try:
            res = evaluate(sym, day, label)
        except Exception as e:
            print(f"  {sym}: {e}")
            res = None
        if res:
            open_signal(st, sym, res[0], res[1], scan_ts)


def main_loop():
    V.HEAD = V.load_keys()
    V.get_bars = live_get_bars
    R.V.get_bars = live_get_bars
    print(f"{CHANNEL_TITLE} — الماسح الحي شغال. Ctrl+C للإيقاف.")
    st, countries, last_watch, last_track = None, {}, None, None
    while True:
        now = pd.Timestamp.now(tz=NY)
        day, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        try:
            if now.weekday() >= 5:
                time.sleep(300)
                continue
            if st is None or st["day"] != day:
                st = load_state(day)
                if not st["universe"]:
                    print("بيجهّز قايمة الأسهم اللي سعرها في الحدود...")
                    st["universe"] = price_universe(day)
                    print(f"  {len(st['universe'])} سهم")
                    save_state(st)
            # ١) تجميع الأسهم الطايرة كل ٥ دقايق
            if WATCH_FROM <= hm <= WATCH_TO and (last_watch is None or (now - last_watch).total_seconds() >= 300):
                new = movers_now(st["universe"]) - set(st["watch"])
                if new:
                    countries.update(A.country_of(sorted(new)))
                    st["watch"] += sorted(new)
                    print(f"{hm}: أسهم طايرة جديدة: {', '.join(sorted(new))}")
                last_watch = now
                save_state(st)
            # ٢) الفحص بعد قفلة :15 و:45 بدقيقتين
            if now.minute in (17, 47):
                scan_ts = now.floor("min") - timedelta(minutes=2)
                label = scan_ts.strftime("%H:%M")
                if SCAN_FIRST <= label <= SCAN_LAST and st["last_scan"] != label:
                    st["last_scan"] = label
                    for s in st["watch"]:
                        if s not in countries:
                            countries.update(A.country_of([s]))
                    scan(st, scan_ts, day, countries)
                    save_state(st)
            # ٣) متابعة الصفقات كل دقيقتين
            if st["open"] and hm <= TRACK_TO and (last_track is None or (now - last_track).total_seconds() >= 120):
                update_open(st, lambda s, d: live_minutes(s, d))
                last_track = now
                save_state(st)
            # ٣ب) ٣:٥٥ العصر: نقفل اللي لسه مفتوح
            if hm >= CLOSE_AT and not st.get("eod_closed"):
                if st["open"]:
                    update_open(st, lambda s, d: live_minutes(s, d))
                close_eod(st)
                save_state(st)
            # ٤) حصاد اليوم
            if hm >= SUMMARY_AT and not st["summary_sent"]:
                if st["open"]:
                    update_open(st, lambda s, d: live_minutes(s, d))
                harvest(st)
                st["summary_sent"] = True
                save_state(st)
        except Exception as e:
            print("خطأ (هكمّل):", e)
        time.sleep(20)


def replay(day, send=False):
    """يعيد يوم قديم بالبيانات المحفوظة، كأن الماسح كان شغال. مع send بيبعت الرسايل كمان."""
    global DRY_RUN, LOG_FILE, STATE_FILE, SEND_DELAY
    DRY_RUN, LOG_FILE, STATE_FILE = (not send), f"replay_{day}.csv", f"replay_{day}.json"
    if send:
        SEND_DELAY = 1.5
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    V.HEAD = V.load_keys()
    pool = A.candidates([day])[day]
    if os.path.exists("signals_with_gap.csv"):
        g = pd.read_csv("signals_with_gap.csv", dtype=str, keep_default_na=False)
        pool |= {t.strip().upper() for t, d in zip(g["ticker"], g["date"]) if d == day}
    countries = A.country_of(sorted(pool))
    st = {"day": day, "watch": sorted(pool), "signaled": [], "open": [], "done": [], "summary_sent": False}
    t = pd.Timestamp(f"{day} {SCAN_FIRST}", tz=NY)
    while t <= pd.Timestamp(f"{day} {SCAN_LAST}", tz=NY):
        scan(st, t, day, countries)
        t += timedelta(minutes=30)

    def hist(sym, d):
        d0 = pd.Timestamp(d)
        m = ORIG_GET_BARS(sym, "1Min", f"{d0:%Y-%m-%d}T08:00:00Z", f"{d0 + timedelta(days=1):%Y-%m-%d}T01:00:00Z")
        if m.empty:
            return m
        m = m[m.index.date == d0.date()]
        return m[m.index.strftime("%H:%M") < CLOSE_AT]
    update_open(st, hist)
    close_eod(st)
    harvest(st)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--replay")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--chat")
    a = ap.parse_args()
    if a.chat:
        CHAT_ID = a.chat
    if a.dry:
        DRY_RUN = True
    if a.test:
        tg_send(f"✅ {CHANNEL_TITLE}: الماسح متوصل بالقناة.")
    elif a.replay:
        replay(a.replay, send=a.send)
    else:
        main_loop()
