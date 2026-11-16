# -*- coding: utf-8 -*-
"""
الخطوة التالتة: تطبيق قواعد النخبة (اللي طلعت من أيامها) على السوق كله في أيام تانية.

الإشارة = أول قفلة :15 أو :45 السهم يعدّي فيها على كل القواعد (إشارة واحدة لكل سهم في اليوم).
الخروج زي النخبة: الهدف الأول +١٢٪، التاني +٢٥٪، الوقف −١٢٪ (الوقف الأول لو في نفس الدقيقة).

التشغيل:
    py nokhba_apply.py                                   (من ١ سبتمبر لـ ١٣ نوفمبر)
    py nokhba_apply.py --start 2026-08-01 --end 2026-11-13

الملفات:
    apply_signals.csv   كل الإشارات بنتايجها
"""
import argparse
import json
import os
import time
from datetime import timedelta

import numpy as np
import pandas as pd
import requests

import nokhba_vol as V
import nokhba_rules as R

NY, CAIRO = V.NY, V.CAIRO

# القواعد الصارمة اللي طلعت من أيام النخبة (أول ٦ خطوات؛ الخطوتين ٧ و٨ كانوا واسعين ومش بيفرقوا)
RULES = [
    ("أعلى صعود %", 20, 10000),
    ("قيمة التداول (مليون $)", 4.27, 133.22),
    ("إغلاق أمس", 0.4737, 3.01),
    ("أعمق تصحيح %", 15.8, 64.4),
    ("فوق متوسط السعر بالحجم %", -11.2, 7.1),
    ("الصعود دلوقتي %", -2.1, 66.8),
    ("RSI خمس دقايق", 45.9, 69.1),
]
# طريقتين جوّه النخبة (طلعوا من أيامها): الإشارة = القواعد السبعة + (الطريقة أ أو الطريقة ب)
# أ: سهم لسه نشيط (مدى آخر ساعة ونص ساعة واسع) والسعر تحت متوسط السعر بالحجم أو عنده
# ب: سهم هادي في آخر نص ساعة، والآر إس آي من ٤٩ لـ ٦٩
SETUPS = {
    "أ": [
        ("فوق القاع %", 3.3, 31.9),
        ("دقايق من القمة", 6.0, 312.0),
        ("مدى آخر ساعة %", 9.5, 124.5),
        ("مدى آخر نص ساعة %", 7.3, 48.5),
        ("فوق متوسط السعر بالحجم %", -11.2, 2.1),
        ("إغلاق أمس", 0.5723, 2.89),
        ("chg30", -9.398496240601506, 9.66542750929369),
    ],
    "ب": [
        ("تحت القمة %", 12.9, 46.8),
        ("مدى آخر نص ساعة %", 4.3, 18.5),
        ("حجم آخر نص ساعة ÷ اللي قبلها", 0.31, 3.23),
        ("قيمة التداول (مليون $)", 9.3, 73.45),
        ("فوق متوسط السعر بالحجم %", -8.7, 7.1),
        ("RSI خمس دقايق", 48.9, 69.1),
    ],
}

# شروط بتشيل أسهم مش شبه النخبة (كل شرط اتطلّع من سهم زيادة وما بيشيلش ولا سهم من أسهم النخبة)
# الصورة بتتشال لو كل أجزاء الشرط صح مع بعض
EXCLUDES = [
    # ١) فرجت ٢٧ أكتوبر: إغلاق أمس دولار أو أكتر، والقاع اتعمل في آخر ٤ دقايق
    [("إغلاق أمس", 1.0, 1e9), ("دقايق من القاع", -1, 4)],
    # ٢) مرلين ١٣ نوفمبر: سهم أقل من دولار وسعره فوق متوسط السعر بالحجم بـ١٪ أو أكتر
    #    (أسهم النخبة اللي تحت الدولار كلها كانت عند المتوسط أو تحته)
    [("إغلاق أمس", 0, 0.9999), ("فوق متوسط السعر بالحجم %", 1.0, 1e9)],
    # ٣) آراي ١٣ نوفمبر: السعر اتضاعف أكتر من ٥ مرات في آخر ٥ أيام قبل النهارده (+٤٠٠٪ أو أكتر)
    #    (أعلى سهم عند النخبة كان +٣٠٧٪، وآراي كان +٢٢١٥٪، غالبًا تجميع أسهم أو طلعة كبيرة خلصت)
    [("تغير آخر ٥ أيام قبلها %", 400, 1e12)],
    # ٤) إس إل إن دي ١٣ نوفمبر: سهم تحت ٠٫٦٣ دولار وتصحيحه من القمة ٢١٪ أو أقل
    #    (أسهم النخبة الرخيصة كانت مصحّحة ٣٣٪ أو أكتر)
    [("إغلاق أمس", 0, 0.63), ("أعمق تصحيح %", 0, 21.3)],
    # ٥) ستاك ١٣ نوفمبر: تصحيح ١٧٪ أو أقل والـRSI ٦٥ أو أكتر (صحّح شوية وسخن تاني بسرعة)
    [("أعمق تصحيح %", 0, 17.0), ("RSI خمس دقايق", 65.3, 100)],
    # ٦) فوفو ١٩ أكتوبر: قيمة التداول ٥٨ مليون أو أكتر والـRSI ٦٢ أو أكتر
    [("قيمة التداول (مليون $)", 57.9, 1e9), ("RSI خمس دقايق", 61.8, 100)],
    # ٧) الساعة ١٠ الصبح نيويورك (أول ساعة بعد الافتتاح): سهم طلع ٤٩٪ أو أكتر وصحّح ٢٤٫٦٪ أو أكتر
    #    (في الاختبار شال ١١ خسرانة و٢ كسبانين، وشغال في النصين، وما لمسش تي دي آي سي)
    [("دقيقة", 600, 659), ("أعلى صعود %", 49, 1e9), ("أعمق تصحيح %", 24.6, 1e9)],
]


def add_minute(s):
    """عمود «دقيقة» = وقت القفلة بالدقايق من نص الليل بتوقيت نيويورك (١٠:١٥ ← ٦١٥)."""
    hm = s["الوقت نيويورك"].str.split(":", expand=True).astype(int)
    s["دقيقة"] = hm[0] * 60 + hm[1]
    return s

# البلاد المسموحة (بلد الشركة من ياهو). الباقي اتشال لأن أداءه مع بعض كان أضعف.
# كندا وسويسرا فضلوا علشان أكان وإيه دي إكس إن من أسهم النخبة.
COUNTRIES = {"United States", "China", "Singapore", "Hong Kong", "Canada", "Switzerland"}
INFO_CACHE = "cache_info.json"


def country_of(tickers):
    """بلد كل سهم من ياهو (بيتحفظ في cache_info.json نفس ملف nokhba_sector.py)."""
    info = json.load(open(INFO_CACHE, encoding="utf-8")) if os.path.exists(INFO_CACHE) else {}
    todo = [t for t in tickers if t not in info]
    if todo:
        import yfinance as yf
        print(f"  بيجيب بلد {len(todo)} سهم من ياهو...")
        for i, t in enumerate(todo, 1):
            try:
                x = yf.Ticker(t).info or {}
            except Exception:
                x = {}
            info[t] = {"القطاع": x.get("sector") or "?", "الصناعة": x.get("industry") or "?",
                       "البلد": x.get("country") or "?",
                       "الأسهم المتاحة (مليون)": round((x.get("floatShares") or 0) / 1e6, 2) or None,
                       "القيمة السوقية (مليون $)": round((x.get("marketCap") or 0) / 1e6, 1) or None}
            if i % 25 == 0:
                print(f"    {i}/{len(todo)}")
                json.dump(info, open(INFO_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
            time.sleep(0.3)
        json.dump(info, open(INFO_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return {t: info.get(t, {}).get("البلد", "?") for t in tickers}


T1, T2, STOP, SLIP, COST = 0.12, 0.25, 0.12, 0.005, 0.005


def nokhba_targets(sig):
    """أهداف زي النخبة، من سعر الإشارة (مش من سعر الدخول بعد الانزلاق):
    الأول = سعر الإشارة × ١٫١٢ مقرّب لأقرب سنت
    التاني = سعر الإشارة × ١٫٢٥ مقرّب لتحت بسنت (زي ٠٫٨٠ ← ٠٫٧٩ و١٫٣٠ ← ١٫٢٩)"""
    t1 = round(sig * 1.12 + 1e-9, 2)
    t2 = np.floor((sig * 1.25 - 0.005) * 100 + 1e-9) / 100
    return t1, t2


def trading_days(start, end):
    spy = V.get_bars("SPY", "1Day", f"{start}T00:00:00Z", f"{end}T23:59:00Z")
    return sorted({d.strftime("%Y-%m-%d") for d in spy.index})


def candidates(days):
    """من الشموع اليومية: أعلى سعر ≥ ٢٠٪ فوق إغلاق أمس، وإغلاق أمس في حدود القاعدة،
    وقيمة التداول في اليوم كله ≥ حد القاعدة (لأن اللي لحد الإشارة أقل منها أكيد)."""
    syms = R.all_symbols()
    d0 = (pd.Timestamp(days[0]) - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
    d1 = (pd.Timestamp(days[-1]) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    cache = os.path.join(V.CACHE, f"daily_all_{d0[:10]}_{d1[:10]}.json")
    if os.path.exists(cache):
        with open(cache) as f:
            bars = json.load(f)
    else:
        bars = {}
        for i in range(0, len(syms), 100):
            chunk, token = syms[i:i + 100], None
            while True:
                p = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": d0, "end": d1,
                     "limit": 10000, "adjustment": "raw", "feed": "sip"}
                if token:
                    p["page_token"] = token
                r = requests.get(f"{V.BASE}/bars", headers=V.HEAD, params=p, timeout=60)
                if r.status_code == 429:
                    time.sleep(5)
                    continue
                if r.status_code != 200:
                    print(f"  خطأ {r.status_code}")
                    break
                j = r.json()
                for s, b in (j.get("bars") or {}).items():
                    bars.setdefault(s, []).extend(b)
                token = j.get("next_page_token")
                time.sleep(0.32)
                if not token:
                    break
            if (i // 100) % 20 == 0:
                print(f"  الشموع اليومية: {i + len(chunk)}/{len(syms)}")
        os.makedirs(V.CACHE, exist_ok=True)
        with open(cache, "w") as f:
            json.dump(bars, f)
    pc_lo, pc_hi = next(((lo, hi) for f, lo, hi in RULES if f == "إغلاق أمس"), (0, 1e9))
    dv_lo = next((lo for f, lo, hi in RULES if f == "قيمة التداول (مليون $)"), 0)
    out = {d: set() for d in days}
    dayset = set(days)
    for s, b in bars.items():
        df = pd.DataFrame(b)
        if df.empty:
            continue
        df["d"] = pd.to_datetime(df["t"]).dt.tz_convert(NY).dt.strftime("%Y-%m-%d")
        df = df.sort_values("d").reset_index(drop=True)
        for k in range(1, len(df)):
            d = df.loc[k, "d"]
            if d not in dayset:
                continue
            pc = df.loc[k - 1, "c"]
            if not (pc_lo <= pc <= pc_hi):
                continue
            if df.loc[k, "h"] / pc - 1 < 0.18:
                continue
            if df.loc[k, "v"] * df.loc[k, "vw"] / 1e6 < dv_lo * 0.5:   # هامش علشان ما قبل الافتتاح
                continue
            out[d].add(s)
    return out


def simulate(sym, day, ts_str, entry):
    d0 = pd.Timestamp(day)
    mins = V.get_bars(sym, "1Min", f"{d0:%Y-%m-%d}T08:00:00Z", f"{d0 + timedelta(days=1):%Y-%m-%d}T01:00:00Z")
    ts = pd.Timestamp(f"{day} {ts_str}", tz=NY)
    after = mins[(mins.index >= ts) & (mins.index.date == d0.date())]
    e = entry * (1 + SLIP)
    t1, t2 = nokhba_targets(entry)          # الأهداف من سعر الإشارة
    lo, hi = after["Low"].values, after["High"].values
    big = len(lo) + 1
    i_s = np.argmax(lo <= e * (1 - STOP)) if (lo <= e * (1 - STOP)).any() else big
    i_1 = np.argmax(hi >= t1) if (hi >= t1).any() else big
    i_2 = np.argmax(hi >= t2) if (hi >= t2).any() else big
    if i_s <= i_1:
        return ("وقف خسارة" if i_s < big else "مفتوحة"), round(e, 4)
    if i_2 < i_s:
        return "الهدف الثاني", round(e, 4)
    return ("وصل الهدف الأول ثم الوقف" if i_s < big else "الهدف الأول"), round(e, 4)


def line(df, label, ndays):
    d = df[df["result"] != "مفتوحة"]
    if d.empty:
        return f"{label}: مفيش"
    w = d["result"].isin(V.WIN)
    pnl = (np.where(w, T1, -STOP) - COST).mean() * 100
    return (f"{label}: {len(df)} إشارة ({len(df) / ndays:.1f} في اليوم)، خلصوا {len(d)}، "
            f"نجاح {w.mean():.0%}، متوسط الصفقة {pnl:+.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-01")
    ap.add_argument("--end", default="2026-11-13")
    ap.add_argument("--two", action="store_true", help="الطريقتين أ و ب بدل القواعد السبعة لوحدها")
    ap.add_argument("--from", dest="from_t", default="", help="أول قفلة نبدأ نوصّي منها، زي 11:45")
    a = ap.parse_args()
    V.HEAD = V.load_keys()

    days = trading_days(a.start, a.end)
    print(f"{len(days)} يوم تداول من {days[0]} لـ {days[-1]}")
    pool = candidates(days)
    if os.path.exists("signals_with_gap.csv"):
        g = pd.read_csv("signals_with_gap.csv", dtype=str, keep_default_na=False)
        for t, d in zip(g["ticker"], g["date"]):
            if d in pool:
                pool[d].add(t.strip().upper())
    todo = [(t, d) for d in days for t in sorted(pool[d])]
    ctry = country_of(sorted({t for t, _ in todo}))
    n0 = len(todo)
    todo = [(t, d) for t, d in todo if ctry.get(t) in COUNTRIES]
    print(f"شرط البلد شال {n0 - len(todo)} سهم×يوم")
    print(f"{len(todo)} سهم×يوم مرشح ({len(todo) / len(days):.0f} في اليوم) — بيحمّل شموع الدقيقة...")

    sigs = []
    for i, (t, d) in enumerate(todo, 1):
        try:
            rows = R.snapshots(t, d)
        except Exception as e:
            print(f"  {t} {d}: {e}")
            rows = []
        if rows:
            s = pd.DataFrame(rows)
            s["chg30"] = (s["السعر"] / s["السعر"].shift(1) - 1).fillna(0) * 100
            s = add_minute(s)
            if a.from_t:
                s = s[s["الوقت نيويورك"] >= a.from_t].reset_index(drop=True)
            m = R.passes(s, RULES)
            for ex in EXCLUDES:
                m = m & ~R.passes(s, ex)
            if a.two:
                ma, mb = m & R.passes(s, SETUPS["أ"]), m & R.passes(s, SETUPS["ب"])
                m = ma | mb
            if m.any():
                r = s[m].iloc[0]
                setup = ("أ+ب" if ma[m][0] and mb[m][0] else ("أ" if ma[m][0] else "ب")) if a.two else ""
                res, e = simulate(t, d, r["الوقت نيويورك"], r["السعر"])
                ts = pd.Timestamp(f"{d} {r['الوقت نيويورك']}", tz=NY)
                sigs.append({"date": d, "ticker": t, "time_ny": r["الوقت نيويورك"],
                             "time_cairo": (ts + timedelta(minutes=1)).tz_convert(CAIRO).strftime("%H:%M"),
                             "entry": e, **{f: r[f] for f, _, _ in RULES}, "الطريقة": setup, "result": res})
        if i % 100 == 0:
            print(f"  {i}/{len(todo)}  (إشارات لحد دلوقتي: {len(sigs)})")

    df = pd.DataFrame(sigs)
    if df.empty:
        print("مفيش ولا إشارة")
        return
    nk = pd.read_csv("nokhba_signals.csv", dtype=str, keep_default_na=False)
    nk_keys = set(zip(nk["ticker"].str.strip().str.upper(), nk["date"].str.strip()))
    nk_days = set(nk["date"].str.strip())
    df["سهم نخبة"] = [int((t, d) in nk_keys) for t, d in zip(df["ticker"], df["date"])]
    df["يوم نخبة"] = df["date"].isin(nk_days).astype(int)
    df = df.sort_values(["date", "time_ny"])
    out_name = "apply2_signals.csv" if a.two else "apply_signals.csv"
    if a.from_t:
        out_name = out_name.replace(".csv", f"_from{a.from_t.replace(':', '')}.csv")
    df.to_csv(out_name, index=False, encoding="utf-8-sig")

    split = days[len(days) // 2]
    other = [d for d in days if d not in nk_days]
    print("\n========== قواعد النخبة على السوق ==========")
    print(line(df, "كل الأيام", len(days)))
    print(line(df[df["يوم نخبة"] == 0], "الأيام اللي ما اتعلمناش منها (الاختبار الحقيقي)", len(other)))
    print(line(df[(df["يوم نخبة"] == 0) & (df["date"] < split)], f"  النص الأول (قبل {split})",
               len([d for d in other if d < split])))
    print(line(df[(df["يوم نخبة"] == 0) & (df["date"] >= split)], "  النص التاني",
               len([d for d in other if d >= split])))
    print(line(df[df["يوم نخبة"] == 1], "أيام النخبة الخمسة", len(nk_days & set(days))))
    print(line(df[(df["يوم نخبة"] == 1) & (df["سهم نخبة"] == 0)], "  الأسهم الزيادة في أيام النخبة",
               len(nk_days & set(days))))
    print("\nعدد الإشارات في كل يوم:")
    print(df.groupby("date").size().describe().round(1).to_string())
    if a.two:
        t = df[df["يوم نخبة"] == 0]
        for k in ("أ", "ب", "أ+ب"):
            print(line(t[t["الطريقة"] == k], f"  الطريقة {k} (الأيام الجديدة)", len(other)))
        nk_rows = df[df["سهم نخبة"] == 1][["date", "ticker", "time_ny", "الطريقة", "result"]]
        print("\nأسهم النخبة:")
        print(nk_rows.to_string(index=False))
    print(f"\nالتفاصيل في {out_name}")


if __name__ == "__main__":
    main()
