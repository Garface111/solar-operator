"""READ-ONLY bake-off: does insolation-weighted bill proration beat flat
proration at matching Crown's monthly numbers? Recomputes the 4 arrays' monthly
kWh from their bills two ways and prints mean |Δ| vs Crown. Writes nothing."""
import json, math
from collections import defaultdict
from datetime import date, timedelta, datetime
from sqlalchemy import select
from api.db import SessionLocal
from api.models import Client, Array, UtilityAccount, Bill

TENANT = "ten_2274f94eac1050b9"
DEFAULT_LAT = 44.5  # Vermont; used when the array isn't geocoded
CROWN = json.loads(r'''
{"Chester":{"2024-07":28.468,"2024-08":24.852,"2024-09":25.72,"2024-10":24.825,"2024-11":17.275,"2024-12":12.673,"2025-01":15.672,"2025-02":16.116,"2025-03":22.69,"2025-04":24.632,"2025-05":20.973,"2025-06":26.745,"2025-07":29.21,"2025-08":29.73,"2025-09":25.723,"2025-10":22.675,"2025-11":12.528,"2025-12":11.62,"2026-01":12.608,"2026-02":16.924,"2026-03":21.061},
"Tannery Brook":{"2024-07":21.951,"2024-08":16.956,"2024-09":17.732,"2024-10":15.331,"2024-11":7.841,"2024-12":1.935,"2025-01":3.854,"2025-02":1.297,"2025-03":14.779,"2025-04":17.552,"2025-05":15.831,"2025-06":19.067,"2025-07":17.981,"2025-08":17.307,"2025-09":17.952,"2025-10":13.648,"2025-11":6.109,"2025-12":0.894,"2026-01":1.341,"2026-02":1.918,"2026-03":13.371},
"Timberworks":{"2024-07":28.35,"2024-08":22.932,"2024-09":22.059,"2024-10":23.387,"2024-11":11.526,"2024-12":4.298,"2025-01":6.985,"2025-02":4.134,"2025-03":20.656,"2025-04":21.958,"2025-05":20.488,"2025-06":25.805,"2025-07":27.499,"2025-08":26.947,"2025-09":27.564,"2025-10":18.919,"2025-11":8.82,"2025-12":2.454,"2026-01":4.043,"2026-02":7.545,"2026-03":15.518},
"Waterford":{"2024-07":28.752,"2024-08":23.255,"2024-09":25.599,"2024-10":20.856,"2024-11":9.955,"2024-12":4.027,"2025-01":11.349,"2025-02":9.894,"2025-03":20.811,"2025-04":21.855,"2025-05":20.862,"2025-06":25.699,"2025-07":27.663,"2025-08":28.218,"2025-09":26.351,"2025-10":18.167,"2025-11":7.721,"2025-12":3.37,"2026-01":4.539,"2026-02":8.801,"2026-03":18.423}}
''')

def _extraterrestrial_ra(d: date, lat_deg: float) -> float:
    """FAO-56 daily extraterrestrial radiation Ra (relative clear-sky insolation
    proxy). Captures the seasonal day-to-day shape; only relative values matter."""
    phi = math.radians(lat_deg)
    J = d.timetuple().tm_yday
    dr = 1 + 0.033 * math.cos(2 * math.pi / 365 * J)
    dec = 0.409 * math.sin(2 * math.pi / 365 * J - 1.39)
    x = -math.tan(phi) * math.tan(dec)
    x = max(-1.0, min(1.0, x))          # clamp for high-lat winter
    ws = math.acos(x)
    ra = (24 * 60 / math.pi) * 0.0820 * dr * (
        ws * math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.sin(ws))
    return max(ra, 0.0)

def _to_date(x):
    if x is None: return None
    return x.date() if isinstance(x, datetime) else x

def prorate(bill, lat, weighted):
    if not bill.kwh_generated or bill.kwh_generated <= 0: return {}
    s, e = _to_date(bill.period_start), _to_date(bill.period_end)
    if s is None or e is None or e < s: return {}
    days = []
    d = s
    while d <= e:
        days.append(d); d += timedelta(days=1)
    if weighted:
        w = [_extraterrestrial_ra(x, lat) for x in days]
        tot = sum(w) or 1.0
        w = [x / tot for x in w]
    else:
        w = [1.0 / len(days)] * len(days)
    out = defaultdict(float)
    for x, wt in zip(days, w):
        out[(x.year, x.month)] += float(bill.kwh_generated) * wt
    return out

with SessionLocal() as db:
    cids = [c.id for c in db.execute(select(Client).where(Client.tenant_id==TENANT)).scalars().all()]
    arrays = {a.name: a for a in db.execute(select(Array).where(Array.client_id.in_(cids))).scalars().all()
              if a.name in CROWN}
    overall = {"flat": [], "weighted": []}
    for name in ["Chester","Tannery Brook","Timberworks","Waterford"]:
        arr = arrays[name]
        lat = arr.latitude if arr.latitude is not None else DEFAULT_LAT
        acct_ids = [a for (a,) in db.execute(select(UtilityAccount.id)
                    .where(UtilityAccount.array_id==arr.id)).all()]
        bills = db.execute(select(Bill).where(Bill.account_id.in_(acct_ids),
                 Bill.kwh_generated.isnot(None), Bill.kwh_generated>0)).scalars().all()
        flat = defaultdict(float); wtd = defaultdict(float)
        for b in bills:
            for k,v in prorate(b, lat, False).items(): flat[k]+=v
            for k,v in prorate(b, lat, True).items():  wtd[k]+=v
        print(f"\n=== {name}  (lat={lat:.2f}{'*' if arr.latitude is None else ''}, {len(bills)} bills)")
        print(f"  {'month':8} {'crown':>7} {'flat':>7} {'wtd':>7} {'|Δflat|':>8} {'|Δwtd|':>8}")
        af=[]; aw=[]
        for mk in sorted(CROWN[name]):
            y,m = int(mk[:4]), int(mk[5:7])
            c = CROWN[name][mk]
            fo = flat.get((y,m),0)/1000.0
            wo = wtd.get((y,m),0)/1000.0
            df, dw = abs(fo-c), abs(wo-c)
            af.append(df); aw.append(dw)
            print(f"  {mk:8} {c:7.2f} {fo:7.2f} {wo:7.2f} {df:8.2f} {dw:8.2f}")
        mf, mw = sum(af)/len(af), sum(aw)/len(aw)
        overall["flat"]+=af; overall["weighted"]+=aw
        print(f"  MEAN |Δ|: flat={mf:.2f}  weighted={mw:.2f}  "
              f"improvement={((mf-mw)/mf*100 if mf else 0):+.0f}%")
    MF = sum(overall["flat"])/len(overall["flat"])
    MW = sum(overall["weighted"])/len(overall["weighted"])
    print("\n" + "="*60)
    print(f"OVERALL mean |Δ| vs Crown: flat={MF:.2f} MWh  weighted={MW:.2f} MWh  "
          f"({(MF-MW)/MF*100:+.0f}% error reduction)")
    # REC-count impact: how many months' floor(MWh) change vs flat / match crown
    print("(* = array not geocoded, used VT default latitude)")
