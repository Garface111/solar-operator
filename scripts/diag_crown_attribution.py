"""READ-ONLY: test how bills SHOULD map to months to match Crown. Compares 4
attribution rules against Crown ground truth (mean |Δ| MWh):
  flat      = prorate flat across calendar days (current behavior)
  end_month = whole bill -> month containing period_end (the read/close month)
  start_month = whole bill -> month containing period_start
  end_offset= whole bill -> month containing (period_end shifted by N days)
Also dumps a few bills' period_start/end so we can see GMP's real cycle."""
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from sqlalchemy import select
from api.db import SessionLocal
from api.models import Client, Array, UtilityAccount, Bill

TENANT = "ten_2274f94eac1050b9"
CROWN = json.loads(r'''
{"Chester":{"2024-07":28.468,"2024-08":24.852,"2024-09":25.72,"2024-10":24.825,"2024-11":17.275,"2024-12":12.673,"2025-01":15.672,"2025-02":16.116,"2025-03":22.69,"2025-04":24.632,"2025-05":20.973,"2025-06":26.745,"2025-07":29.21,"2025-08":29.73,"2025-09":25.723,"2025-10":22.675,"2025-11":12.528,"2025-12":11.62,"2026-01":12.608,"2026-02":16.924,"2026-03":21.061},
"Tannery Brook":{"2024-07":21.951,"2024-08":16.956,"2024-09":17.732,"2024-10":15.331,"2024-11":7.841,"2024-12":1.935,"2025-01":3.854,"2025-02":1.297,"2025-03":14.779,"2025-04":17.552,"2025-05":15.831,"2025-06":19.067,"2025-07":17.981,"2025-08":17.307,"2025-09":17.952,"2025-10":13.648,"2025-11":6.109,"2025-12":0.894,"2026-01":1.341,"2026-02":1.918,"2026-03":13.371},
"Timberworks":{"2024-07":28.35,"2024-08":22.932,"2024-09":22.059,"2024-10":23.387,"2024-11":11.526,"2024-12":4.298,"2025-01":6.985,"2025-02":4.134,"2025-03":20.656,"2025-04":21.958,"2025-05":20.488,"2025-06":25.805,"2025-07":27.499,"2025-08":26.947,"2025-09":27.564,"2025-10":18.919,"2025-11":8.82,"2025-12":2.454,"2026-01":4.043,"2026-02":7.545,"2026-03":15.518},
"Waterford":{"2024-07":28.752,"2024-08":23.255,"2024-09":25.599,"2024-10":20.856,"2024-11":9.955,"2024-12":4.027,"2025-01":11.349,"2025-02":9.894,"2025-03":20.811,"2025-04":21.855,"2025-05":20.862,"2025-06":25.699,"2025-07":27.663,"2025-08":28.218,"2025-09":26.351,"2025-10":18.167,"2025-11":7.721,"2025-12":3.37,"2026-01":4.539,"2026-02":8.801,"2026-03":18.423}}
''')

def _d(x):
    if x is None: return None
    return x.date() if isinstance(x, datetime) else x

def month_of_shift(dt, days):
    dt2 = dt + timedelta(days=days)
    return (dt2.year, dt2.month)

def score(assign_fn, arrays, db):
    """assign_fn(bill)-> {(y,m): kwh}. return per-array + overall mean|Δ|."""
    alld = []
    per = {}
    for name in ["Chester","Tannery Brook","Timberworks","Waterford"]:
        arr = arrays[name]
        acct_ids = [a for (a,) in db.execute(select(UtilityAccount.id).where(UtilityAccount.array_id==arr.id)).all()]
        bills = db.execute(select(Bill).where(Bill.account_id.in_(acct_ids),
                 Bill.kwh_generated.isnot(None), Bill.kwh_generated>0)).scalars().all()
        m = defaultdict(float)
        for b in bills:
            for k,v in assign_fn(b).items(): m[k]+=v
        ad=[]
        for mk in CROWN[name]:
            y,mm = int(mk[:4]), int(mk[5:7])
            ad.append(abs(m.get((y,mm),0)/1000.0 - CROWN[name][mk]))
        per[name]=sum(ad)/len(ad); alld+=ad
    return per, sum(alld)/len(alld)

def flat(b):
    s,e = _d(b.period_start), _d(b.period_end)
    if not b.kwh_generated or b.kwh_generated<=0: return {}
    if s is None or e is None or e<s: return {}
    out=defaultdict(float); d=s; n=(e-s).days+1
    while d<=e: out[(d.year,d.month)]+=float(b.kwh_generated)/n; d+=timedelta(days=1)
    return out
def end_month(b):
    e=_d(b.period_end);
    return {(e.year,e.month):float(b.kwh_generated)} if (e and b.kwh_generated and b.kwh_generated>0) else {}
def start_month(b):
    s=_d(b.period_start)
    return {(s.year,s.month):float(b.kwh_generated)} if (s and b.kwh_generated and b.kwh_generated>0) else {}
def end_shift(days):
    def f(b):
        e=_d(b.period_end)
        return {month_of_shift(e,days):float(b.kwh_generated)} if (e and b.kwh_generated and b.kwh_generated>0) else {}
    return f

with SessionLocal() as db:
    cids=[c.id for c in db.execute(select(Client).where(Client.tenant_id==TENANT)).scalars().all()]
    arrays={a.name:a for a in db.execute(select(Array).where(Array.client_id.in_(cids))).scalars().all() if a.name in CROWN}

    rules = {"flat(current)":flat, "end_month":end_month, "start_month":start_month,
             "end-15d":end_shift(-15), "end-20d":end_shift(-20), "end+10d":end_shift(10)}
    print(f"{'rule':16} {'OVERALL':>8}  " + "  ".join(f"{n[:11]:>11}" for n in CROWN))
    for rn, fn in rules.items():
        per, ov = score(fn, arrays, db)
        print(f"{rn:16} {ov:8.2f}  " + "  ".join(f"{per[n]:11.2f}" for n in CROWN))

    print("\nSample Chester bills (period_start -> period_end, kwh_generated):")
    arr=arrays["Chester"]
    acct_ids=[a for (a,) in db.execute(select(UtilityAccount.id).where(UtilityAccount.array_id==arr.id)).all()]
    bills=db.execute(select(Bill).where(Bill.account_id.in_(acct_ids),Bill.kwh_generated.isnot(None),
            Bill.kwh_generated>0).order_by(Bill.period_end.desc()).limit(8)).scalars().all()
    for b in bills:
        s,e=_d(b.period_start),_d(b.period_end)
        print(f"  {s} -> {e}  ({(e-s).days+1}d)  kwh={b.kwh_generated}  bill_date={_d(b.bill_date)}")
