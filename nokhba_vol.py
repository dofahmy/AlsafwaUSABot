# -*- coding: utf-8 -*-
"""
حجم التداول لحد لحظة الإشارة (شامل ما قبل الافتتاح) من ألباكا.

المفاتيح بتتقري من ملف alpaca_keys.txt في نفس المجلد:
  APCA_API_KEY_ID=...
  APCA_API_SECRET_KEY=...

وضعين:
  py nokhba_vol.py --nokhba
      حجم التداول وقت كل توصية من توصيات النخبة (nokhba_signals.csv)

  py nokhba_vol.py --test signals_K2_nokhba_once.csv
      نفس الحسبة لكل إشارات الماسح في الملف ده (الشهر كله)،
      وبعدين نسبة النجاح حسب الحجم، علشان نشوف شرط الحجم بيفرق ولا لأ.
"""
import argparse
import json
import os
import sys
import time
from datetime import timedelta

import numpy as np
import pandas as pd
import requests

NY = "America/New_York"
CAIRO = "Africa/Cairo"
CACHE = "cache_alpaca"
BASE = "https://data.alpaca.markets/v2/stocks"
WIN = {"الهدف الأول", "الهدف الثاني", "الهدف الثالث", "وصل الهدف الأول ثم الوقف"}


def load_keys(path="alpaca_keys.txt"):
    kid, sec = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if kid and sec:                       # على السيرفر: المفاتيح من المتغيرات مش من ملف
        return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}
    if not os.path.exists(path):
        sys.exit("مش لاقي alpaca_keys.txt في نفس المجلد.")
    keys = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            if "=" in line:
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip()
    kid, sec = keys.get("APCA_API_KEY_ID"), keys.get("APCA_API_SECRET_KEY")
    if not kid or not sec:
        sys.exit("الملف لازم يكون فيه السطرين APCA_API_KEY_ID و APCA_API_SECRET_KEY")
    return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}


HEAD = None


def get_bars(sym, timeframe, start, end, adjustment="raw"):
    """شموع من ألباكا (افتراضيًا بالأسعار الأصلية من غير تعديل الدمج/التقسيم)، مع حفظ مؤقت."""
    os.makedirs(CACHE, exist_ok=True)
    tag = "" if adjustment == "raw" else f"_{adjustment}"
    key = f"{sym}_{timeframe}_{start[:10]}_{end[:10]}{tag}.json"
    path = os.path.join(CACHE, key)
    if os.path.exists(path):
        with open(path) as f:
            rows = json.load(f)
    else:
        rows, token = [], None
        for _ in range(20):
            params = {"timeframe": timeframe, "start": start, "end": end, "limit": 10000,
                      "adjustment": adjustment, "feed": "sip"}
            if token:
                params["page_token"] = token
            for attempt in range(5):
                r = requests.get(f"{BASE}/{sym}/bars", headers=HEAD, params=params, timeout=30)
                if r.status_code == 429:
                    time.sleep(5)
                    continue
                break
            if r.status_code in (401, 403):
                sys.exit(f"ألباكا رفضت المفاتيح ({r.status_code}): {r.text[:200]}")
            if r.status_code != 200:
                print(f"  {sym}: خطأ {r.status_code} {r.text[:120]}")
                break
            j = r.json()
            rows += j.get("bars") or []
            token = j.get("next_page_token")
            time.sleep(0.32)  # الحد المجاني 200 طلب في الدقيقة
            if not token:
                break
        with open(path, "w") as f:
            json.dump(rows, f)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(NY)
    return df.set_index("t").rename(columns={"o": "Open", "h": "High", "l": "Low",
                                              "c": "Close", "v": "Volume"})


def get_bars_adj(sym, start, end):
    """شموع يومية معدّلة للتقسيم والدمج (زي تريدنج فيو) للمؤشرات."""
    return get_bars(sym, "1Day", start, end, adjustment="split")


def volume_features(sym, day, sig_ts, entry=None):
    """كل الحسابات لحد لحظة الإشارة بالظبط، مفيش أي بيانات من بعدها."""
    d0 = pd.Timestamp(day)
    daily = get_bars(sym, "1Day", (d0 - timedelta(days=45)).strftime("%Y-%m-%dT00:00:00Z"),
                     (d0 - timedelta(days=1)).strftime("%Y-%m-%dT23:59:00Z"))
    mins = get_bars(sym, "1Min", f"{d0:%Y-%m-%d}T08:00:00Z", f"{d0 + timedelta(days=1):%Y-%m-%d}T01:00:00Z")
    if daily.empty or mins.empty:
        return None
    daily = daily[daily.index.date < d0.date()]
    if len(daily) < 5:
        return None
    avg_vol = daily["Volume"].iloc[-20:].mean()
    prev_close = daily["Close"].iloc[-1]
    upto = mins[mins.index < sig_ts]  # شمعة الدقيقة اللي بتبدأ قبل الإشارة
    if upto.empty:
        return None
    pre = upto[upto.index.time < pd.Timestamp("09:30").time()]
    last30 = upto[upto.index >= sig_ts - timedelta(minutes=30)]
    cum = upto["Volume"].sum()
    dollar = (upto["Close"] * upto["Volume"]).sum()
    per_min_avg = avg_vol / 390
    price = upto["Close"].iloc[-1]
    return {
        "سعر ألباكا وقت الإشارة": round(price, 4),
        "فرق عن سعر الدخول %": round((price / entry - 1) * 100, 1) if entry else None,
        "إغلاق أمس": round(prev_close, 4),
        "متوسط الحجم اليومي (ألف)": round(avg_vol / 1e3, 1),
        "الحجم لحد الإشارة (ألف)": round(cum / 1e3, 1),
        "الحجم لحد الإشارة ÷ المتوسط": round(cum / avg_vol, 2) if avg_vol else None,
        "حجم ما قبل الافتتاح ÷ المتوسط": round(pre["Volume"].sum() / avg_vol, 2) if avg_vol else None,
        "قيمة التداول لحد الإشارة (مليون $)": round(dollar / 1e6, 2),
        "حجم آخر نص ساعة ÷ العادي": round(last30["Volume"].sum() / (per_min_avg * 30), 1) if avg_vol else None,
    }


def run_nokhba(path):
    sig = pd.read_csv(path, dtype=str, keep_default_na=False)
    rows = []
    for _, g in sig.iterrows():
        t, day = g["ticker"].strip().upper(), g["date"].strip()
        if not day or not g["time_cairo"].strip():
            continue
        sig_ts = pd.Timestamp(f"{day} {g['time_cairo'].strip()}", tz=CAIRO).tz_convert(NY)
        entry = float(g["entry"]) if g["entry"] else None
        f = volume_features(t, day, sig_ts, entry)
        print(f"  {t} {day}", "✓" if f else "✗ مفيش بيانات")
        if not f:
            continue
        rows.append({"السهم": t, "اليوم": day, "الوقت نيويورك": sig_ts.strftime("%H:%M"),
                     "دخول النخبة": entry, **f, "نتيجة النخبة": g.get("bot_result", "")})
    out = pd.DataFrame(rows)
    out.to_csv("nokhba_volume.csv", index=False, encoding="utf-8-sig")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    print("\n========== حجم التداول وقت توصيات النخبة ==========")
    print(out.T.to_string())
    print("\nاتحفظت في nokhba_volume.csv")


def run_test(path):
    s = pd.read_csv(path)
    s = s[s["time_ny"] >= "07:00"].drop_duplicates(["ticker", "date"])
    print(f"عدد الإشارات: {len(s)}")
    feats = []
    for i, r in enumerate(s.itertuples()):
        if i % 20 == 0:
            print(f"  {i + 1}/{len(s)}", flush=True)
        sig_ts = pd.Timestamp(f"{r.date} {r.time_ny}", tz=NY)
        f = volume_features(r.ticker, str(r.date), sig_ts)
        feats.append(f or {})
    v = pd.concat([s.reset_index(drop=True), pd.DataFrame(feats)], axis=1)
    v.to_csv("signals_with_volume.csv", index=False, encoding="utf-8-sig")
    v = v[v["result"] != "مفتوحة"].dropna(subset=["الحجم لحد الإشارة ÷ المتوسط"])
    v["win"] = v["result"].isin(WIN)

    def line(d, name):
        if len(d) == 0:
            return f"{name}: مفيش"
        pnl = np.where(d["win"], 12, -12).mean() - 0.5
        days = d["date"].nunique()
        return (f"{name}: {len(d)} إشارة ({len(d) / max(days, 1):.1f} في اليوم)، "
                f"نجاح {d['win'].mean():.0%}، متوسط الصفقة {pnl:+.2f}%")

    print("\n========== النتيجة حسب الحجم لحد لحظة الإشارة ==========")
    print(line(v, "كل الإشارات"))
    col = "الحجم لحد الإشارة ÷ المتوسط"
    for thr in [0.5, 1, 2, 3, 5, 10]:
        print(line(v[v[col] >= thr], f"الحجم ≥ {thr} × المتوسط"))
    print()
    v["ربع"] = pd.qcut(v[col], 4, duplicates="drop")
    for q, d in v.groupby("ربع", observed=True):
        print(line(d, f"الحجم بين {q.left:.2f} و {q.right:.2f}"))
    print()
    col2 = "قيمة التداول لحد الإشارة (مليون $)"
    for thr in [0.5, 1, 2, 5]:
        print(line(v[v[col2] >= thr], f"قيمة التداول ≥ {thr} مليون $"))
    print("\nالتفاصيل في signals_with_volume.csv")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--nokhba", nargs="?", const="nokhba_signals.csv")
    ap.add_argument("--test", nargs="?", const="signals_K2_nokhba_once.csv")
    a = ap.parse_args()
    HEAD = load_keys()
    if a.nokhba:
        run_nokhba(a.nokhba)
    if a.test:
        run_test(a.test)
    if not (a.nokhba or a.test):
        print("اختاري: --nokhba أو --test")
