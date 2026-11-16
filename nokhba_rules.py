# -*- coding: utf-8 -*-
"""
استخراج قواعد النخبة من أيامها نفسها.

الفكرة:
  ١) في كل يوم من أيام النخبة، نجيب كل الأسهم اللي طارت في اليوم ده (مش بس اللي ماسحنا طلّعها).
  ٢) لكل سهم نصوّر حالته عند كل قفلة :15 و:45 (نفس مواعيد النخبة) بأرقام من قبل اللحظة دي بس.
  ٣) أسهم النخبة في وقت توصيتها = «اختارتها». كل الباقي = «سابته».
  ٤) ندوّر على أبسط قواعد تمسك كل أسهمها وتشيل أكبر عدد من الباقي.

التشغيل (من نفس الفولدر اللي فيه alpaca_keys.txt و nokhba_vol.py):
    py nokhba_rules.py

الملفات اللي بتطلع:
    rules_snapshots.csv   كل الصور (سهم × يوم × وقت) بكل الأرقام
    rules_nokhba.csv      أرقام أسهم النخبة وقت توصيتها
    rules_steps.csv       القواعد خطوة بخطوة وكام سهم تاني بيعدّي بعد كل خطوة
    rules_left.csv        الأسهم التانية اللي لسه بتعدّي على كل القواعد (نبصلها بعينينا)
"""
import json
import os
from datetime import timedelta

import numpy as np
import pandas as pd
import requests

import nokhba_vol as V

NY, CAIRO = V.NY, V.CAIRO
OPEN_T = pd.Timestamp("09:30").time()
MIN_DAY_GAIN = 0.18          # أي سهم أعلى سعر له في اليوم ١٨٪ فوق إغلاق أمس يبقى مرشح
MIN_HOD_NOW = 20             # الشرط الأساسي: طاير ٢٠٪ على الأقل لحد اللحظة دي (إف آر إم آي كان ٢٣٪)
FIRST_SNAP, LAST_SNAP = "07:15", "15:45"
ASSETS_CACHE = os.path.join(V.CACHE, "assets.json")


# ---------------- قايمة كل الأسهم ----------------
def all_symbols():
    if os.path.exists(ASSETS_CACHE):
        with open(ASSETS_CACHE) as f:
            return json.load(f)
    r = requests.get("https://paper-api.alpaca.markets/v2/assets",
                     headers=V.HEAD, params={"status": "active", "asset_class": "us_equity"}, timeout=60)
    if r.status_code != 200:
        print(f"  مش قادر أجيب قايمة الأسهم ({r.status_code})، هكمّل بقايمة الماسح بس")
        return []
    ok = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}
    syms = sorted(a["symbol"] for a in r.json()
                  if a.get("exchange") in ok and a.get("tradable") and a["symbol"].isalpha())
    os.makedirs(V.CACHE, exist_ok=True)
    with open(ASSETS_CACHE, "w") as f:
        json.dump(syms, f)
    return syms


def daily_movers(days):
    """الأسهم اللي أعلى سعر لها في اليوم ≥ ٢٥٪ فوق إغلاق أمس، لكل يوم."""
    syms = all_symbols()
    if not syms:
        return {d: set() for d in days}
    d0 = (pd.Timestamp(min(days)) - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
    d1 = (pd.Timestamp(max(days)) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    cache = os.path.join(V.CACHE, f"daily_all_{d0[:10]}_{d1[:10]}.json")
    if os.path.exists(cache):
        with open(cache) as f:
            bars = json.load(f)
    else:
        bars = {}
        for i in range(0, len(syms), 200):
            chunk = syms[i:i + 200]
            token = None
            while True:
                p = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": d0, "end": d1,
                     "limit": 10000, "adjustment": "raw", "feed": "sip"}
                if token:
                    p["page_token"] = token
                r = requests.get(f"{V.BASE}/bars", headers=V.HEAD, params=p, timeout=60)
                if r.status_code == 429:
                    import time; time.sleep(5); continue
                if r.status_code != 200:
                    print(f"  خطأ {r.status_code} في الشموع اليومية")
                    break
                j = r.json()
                for s, b in (j.get("bars") or {}).items():
                    bars.setdefault(s, []).extend(b)
                token = j.get("next_page_token")
                if not token:
                    break
            if (i // 200) % 10 == 0:
                print(f"  الشموع اليومية: {i + len(chunk)}/{len(syms)}")
        with open(cache, "w") as f:
            json.dump(bars, f)
    out = {d: set() for d in days}
    for s, b in bars.items():
        df = pd.DataFrame(b)
        if df.empty:
            continue
        df["d"] = pd.to_datetime(df["t"]).dt.tz_convert(NY).dt.strftime("%Y-%m-%d")
        df = df.sort_values("d").reset_index(drop=True)
        for d in days:
            k = df.index[df["d"] == d]
            if len(k) and k[0] > 0:
                pc = df.loc[k[0] - 1, "c"]
                if pc > 0 and df.loc[k[0], "h"] / pc - 1 >= MIN_DAY_GAIN:
                    out[d].add(s)
    return out


# ---------------- صور الحالة عند كل :15 و:45 ----------------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def snapshots(sym, day):
    d0 = pd.Timestamp(day)
    daily = V.get_bars(sym, "1Day", (d0 - timedelta(days=45)).strftime("%Y-%m-%dT00:00:00Z"),
                       (d0 - timedelta(days=1)).strftime("%Y-%m-%dT23:59:00Z"))
    mins = V.get_bars(sym, "1Min", f"{d0:%Y-%m-%d}T08:00:00Z", f"{d0 + timedelta(days=1):%Y-%m-%d}T01:00:00Z")
    if daily.empty or mins.empty:
        return []
    daily = daily[daily.index.date < d0.date()]
    mins = mins[mins.index.date == d0.date()]
    if len(daily) < 5 or mins.empty:
        return []
    pc = float(daily["Close"].iloc[-1])
    avg_vol = float(daily["Volume"].iloc[-20:].mean())
    chg5 = pc / float(daily["Close"].iloc[-6]) - 1 if len(daily) >= 6 else np.nan
    b5 = mins.resample("5min").agg({"Close": "last"}).dropna()
    r5 = rsi(b5["Close"])
    reg = mins[mins.index.time >= OPEN_T]
    day_open = float(reg["Open"].iloc[0]) if not reg.empty else None
    tp = (mins["High"] + mins["Low"] + mins["Close"]) / 3
    cum_pv, cum_v = (tp * mins["Volume"]).cumsum(), mins["Volume"].cumsum()

    rows = []
    t = pd.Timestamp(f"{day} {FIRST_SNAP}", tz=NY)
    t_end = pd.Timestamp(f"{day} {LAST_SNAP}", tz=NY)
    while t <= t_end:
        upto = mins[mins.index < t]
        if len(upto) >= 5:
            price = float(upto["Close"].iloc[-1])
            hod_ts, hod = upto["High"].idxmax(), float(upto["High"].max())
            after = upto[upto.index > hod_ts]
            low = float(after["Low"].min()) if not after.empty else hod
            low_ts = after["Low"].idxmin() if not after.empty else hod_ts
            l60 = upto[upto.index >= t - timedelta(minutes=60)]
            l30 = upto[upto.index >= t - timedelta(minutes=30)]
            p30 = upto[(upto.index >= t - timedelta(minutes=60)) & (upto.index < t - timedelta(minutes=30))]
            vw = float(cum_pv.loc[:upto.index[-1]].iloc[-1] / max(cum_v.loc[:upto.index[-1]].iloc[-1], 1))
            rr = r5[r5.index < t]
            opened = t.time() > OPEN_T and day_open is not None
            rows.append({
                "السهم": sym, "اليوم": day, "الوقت نيويورك": t.strftime("%H:%M"),
                "السعر": round(price, 4),
                "الصعود دلوقتي %": round((price / pc - 1) * 100, 1),
                "أعلى صعود %": round((hod / pc - 1) * 100, 1),
                "أعمق تصحيح %": round((1 - low / hod) * 100, 1),
                "تحت القمة %": round((1 - price / hod) * 100, 1),
                "فوق القاع %": round((price / low - 1) * 100, 1),
                "دقايق من القمة": int((t - hod_ts).total_seconds() // 60),
                "دقايق من القاع": int((t - low_ts).total_seconds() // 60),
                "القمة قبل الافتتاح": int(hod_ts.time() < OPEN_T),
                "مدى آخر ساعة %": round((l60["High"].max() / l60["Low"].min() - 1) * 100, 1),
                "مدى آخر نص ساعة %": round((l30["High"].max() / l30["Low"].min() - 1) * 100, 1),
                "حجم آخر نص ساعة ÷ اللي قبلها": round(l30["Volume"].sum() / max(p30["Volume"].sum(), 1), 2),
                "الحجم لحد دلوقتي ÷ المتوسط": round(upto["Volume"].sum() / max(avg_vol, 1), 2),
                "قيمة التداول (مليون $)": round(float((upto["Close"] * upto["Volume"]).sum()) / 1e6, 2),
                "فوق متوسط السعر بالحجم %": round((price / vw - 1) * 100, 1),
                "RSI خمس دقايق": round(float(rr.iloc[-1]), 1) if len(rr) and pd.notna(rr.iloc[-1]) else np.nan,
                "الفجوة عند الافتتاح %": round((day_open / pc - 1) * 100, 1) if opened else np.nan,
                "تحت سعر الافتتاح": (int(price < day_open) if opened else np.nan),
                "تغير آخر ٥ أيام قبلها %": round(chg5 * 100, 1) if pd.notna(chg5) else np.nan,
                "إغلاق أمس": round(pc, 4),
            })
        t += timedelta(minutes=30)
    return rows


FEATURES = ["السعر", "الصعود دلوقتي %", "أعلى صعود %", "أعمق تصحيح %", "تحت القمة %", "فوق القاع %",
            "دقايق من القمة", "دقايق من القاع", "مدى آخر ساعة %", "مدى آخر نص ساعة %",
            "حجم آخر نص ساعة ÷ اللي قبلها", "الحجم لحد دلوقتي ÷ المتوسط", "قيمة التداول (مليون $)",
            "فوق متوسط السعر بالحجم %", "RSI خمس دقايق", "تغير آخر ٥ أيام قبلها %", "إغلاق أمس"]


def nokhba_snap_time(cairo_hm, day):
    """وقت التوصية بالقاهرة ← أقرب قفلة :15 أو :45 قبله بتوقيت نيويورك."""
    ts = pd.Timestamp(f"{day} {cairo_hm}", tz=CAIRO).tz_convert(NY)
    m = 45 if ts.minute >= 45 else (15 if ts.minute >= 15 else -15)
    base = ts.replace(minute=0, second=0)
    return (base + timedelta(minutes=m)).strftime("%H:%M")


def passes(df, rules):
    m = np.ones(len(df), bool)
    for f, lo, hi in rules:
        v = df[f].values.astype(float)
        m &= (v >= lo) & (v <= hi)
    return m


def evaluate(snap, rules, ndays):
    """كام سهم تاني بيعدّي، وأسهم النخبة بتعدّي في وقتها ولا بدري."""
    m = passes(snap, rules)
    hit = snap[m]
    first = hit.sort_values("الوقت نيويورك").groupby(["السهم", "اليوم"]).head(1)
    others = first[first["نخبة"] == 0]
    nk = first[first["نخبة"] == 1]
    on_time = int((nk["الوقت نيويورك"] == nk["وقت النخبة"]).sum())
    early = int((nk["الوقت نيويورك"] < nk["وقت النخبة"]).sum())
    return {"أسهم تانية بتعدّي": len(others), "أسهم تانية في اليوم": round(len(others) / ndays, 1),
            "أسهم النخبة في وقتها": on_time, "أسهم النخبة بدري عن وقتها": early}, others


def main():
    V.HEAD = V.load_keys()
    nk = pd.read_csv("nokhba_signals.csv", dtype=str, keep_default_na=False)
    nk = nk[nk["date"].str.strip() != ""].copy()
    nk["ticker"] = nk["ticker"].str.strip().str.upper()
    nk["date"] = nk["date"].str.strip()
    nk["snap"] = [nokhba_snap_time(c.strip(), d) for c, d in zip(nk["time_cairo"], nk["date"])]
    days = sorted(nk["date"].unique())
    print(f"أيام النخبة: {', '.join(days)}")

    print("بيدوّر على كل الأسهم اللي طارت في الأيام دي...")
    movers = daily_movers(days)
    pool = {d: set(movers[d]) for d in days}
    if os.path.exists("signals_with_gap.csv"):
        g = pd.read_csv("signals_with_gap.csv", dtype=str, keep_default_na=False)
        for t, d in zip(g["ticker"], g["date"]):
            if d in pool:
                pool[d].add(t.strip().upper())
    for t, d in zip(nk["ticker"], nk["date"]):
        pool[d].add(t)
    for d in days:
        print(f"  {d}: {len(pool[d])} سهم مرشح")

    rows = []
    todo = [(t, d) for d in days for t in sorted(pool[d])]
    for i, (t, d) in enumerate(todo, 1):
        try:
            rows += snapshots(t, d)
        except Exception as e:
            print(f"  {t} {d}: {e}")
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}")
    snap = pd.DataFrame(rows)
    # الشرط الأساسي: السهم طاير ٢٠٪ على الأقل في اللحظة دي
    snap = snap[snap["أعلى صعود %"] >= MIN_HOD_NOW].copy()
    key = {(t, d): s for t, d, s in zip(nk["ticker"], nk["date"], nk["snap"])}
    snap["وقت النخبة"] = [key.get((t, d), "") for t, d in zip(snap["السهم"], snap["اليوم"])]
    snap["نخبة"] = (snap["وقت النخبة"] != "").astype(int)
    snap["لحظة توصية النخبة"] = (snap["الوقت نيويورك"] == snap["وقت النخبة"]).astype(int)
    snap.to_csv("rules_snapshots.csv", index=False, encoding="utf-8-sig")

    pos = snap[snap["لحظة توصية النخبة"] == 1]
    pos = pos.merge(nk[["ticker", "date", "bot_result"]], left_on=["السهم", "اليوم"],
                    right_on=["ticker", "date"], how="left").drop(columns=["ticker", "date"])
    pos.to_csv("rules_nokhba.csv", index=False, encoding="utf-8-sig")
    miss = set(key) - set(zip(pos["السهم"], pos["اليوم"]))
    print(f"\nأسهم النخبة اللي ليها صورة وقت توصيتها: {len(pos)} من {len(key)}"
          + (f"  (ناقص: {', '.join(t for t, _ in miss)})" if miss else ""))

    pd.set_option("display.width", 300)
    pd.set_option("display.max_columns", 50)
    print("\n===== أرقام أسهم النخبة وقت التوصية =====")
    print(pos[["السهم", "اليوم", "الوقت نيويورك"] + FEATURES[:12] + ["bot_result"]].to_string(index=False))

    # الصور اللي «سابتها» النخبة: كل الأسهم التانية في كل الأوقات
    neg = snap[snap["نخبة"] == 0]
    ndays = len(days)
    print(f"\nالأسهم التانية اللي كانت طايرة {MIN_HOD_NOW}٪+ في الأيام دي: {neg.groupby(['السهم','اليوم']).ngroups}"
          f" ({neg.groupby(['السهم','اليوم']).ngroups / ndays:.1f} في اليوم)")

    pd.set_option("display.max_colwidth", 60)
    for name, q_lo, q_hi, fname in [("صارمة: كل قاعدة بتمسك كل أسهم النخبة الـ١٨", 0.0, 1.0, "rules_steps.csv"),
                                     ("مرنة: كل قاعدة ممكن تسيب سهم أو اتنين متطرفين", 0.06, 0.94, "rules_steps_loose.csv")]:
        rules = greedy(snap, pos, ndays, q_lo, q_hi)
        st = pd.DataFrame(rules["steps"])
        st.to_csv(fname, index=False, encoding="utf-8-sig")
        print(f"\n===== القواعد خطوة بخطوة — {name} =====")
        print(st.to_string(index=False))
        _, left = evaluate(snap, rules["rules"], ndays)
        out = fname.replace("steps", "left")
        left.to_csv(out, index=False, encoding="utf-8-sig")
        hit = snap[passes(snap, rules["rules"])]
        nk_first = hit[hit["نخبة"] == 1].sort_values("الوقت نيويورك").groupby(["السهم", "اليوم"]).head(1)
        print("\nأسهم النخبة: أول وقت عدّت فيه على القواعد مقابل وقت توصيتهم:")
        allk = pos[["السهم", "اليوم", "الوقت نيويورك"]].rename(columns={"الوقت نيويورك": "وقت النخبة"})
        allk = allk.merge(nk_first[["السهم", "اليوم", "الوقت نيويورك"]].rename(columns={"الوقت نيويورك": "أول وقت عدّت"}),
                          on=["السهم", "اليوم"], how="left").fillna({"أول وقت عدّت": "ما عدّتش"})
        print(allk.to_string(index=False))
        print(f"\nالأسهم التانية اللي لسه بتعدّي ({len(left)}):")
        print(left[["اليوم", "السهم", "الوقت نيويورك"] + FEATURES[:8]].sort_values(["اليوم", "الوقت نيويورك"])
              .to_string(index=False) if not left.empty else "ولا سهم — القواعد بتختار أسهم النخبة بس")


def greedy(snap, pos, ndays, q_lo, q_hi):
    """كل خطوة نضيف الرقم اللي حدوده (من أسهم النخبة) بتقلّل الغلط أكتر.
    الغلط = أسهم تانية بتعدّي + أسهم نخبة بتعدّي بدري عن وقتها + أسهم نخبة ضاعت."""
    def err(r):
        return r["أسهم تانية بتعدّي"] + r["أسهم النخبة بدري عن وقتها"] + 2 * (len(pos) - r["أسهم النخبة في وقتها"] - r["أسهم النخبة بدري عن وقتها"])
    rules, used = [], set()
    res, _ = evaluate(snap, rules, ndays)
    steps = [{"الخطوة": 0, "القاعدة": f"طاير {MIN_HOD_NOW}٪ أو أكتر بس", **res}]
    cur = err(res)
    for step in range(1, 9):
        best = None
        for f in FEATURES:
            if f in used:
                continue
            v = pos[f].dropna()
            if len(v) < len(pos) - 1:
                continue
            lo, hi = float(v.quantile(q_lo)), float(v.quantile(q_hi))
            r, _ = evaluate(snap, rules + [(f, lo, hi)], ndays)
            e = err(r)
            if best is None or e < best[2]:
                best = ((f, lo, hi), r, e)
        if best is None or best[2] >= cur:
            break
        rules.append(best[0])
        used.add(best[0][0])
        cur = best[2]
        f, lo, hi = best[0]
        steps.append({"الخطوة": step, "القاعدة": f"{f} من {lo:g} لـ {hi:g}", **best[1]})
    return {"rules": rules, "steps": steps}


if __name__ == "__main__":
    main()
