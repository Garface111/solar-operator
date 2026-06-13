"""Live proof: pull Bruce's real SolarEdge inverters -> run through the product's
peer-analysis engine. Key from env SE_KEY. No secrets written to disk."""
import os, sys, re, json, time, urllib.request, urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/solar-operator")
from api.inverters.peer_analysis import analyze_cohort

KEY = os.environ["SE_KEY"]; SID = 416160
BASE = "https://monitoringapi.solaredge.com"

def get(path, **params):
    params["api_key"] = KEY
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == 2: raise
            time.sleep(2)

def nameplate(model):
    m = re.search(r"(\d+(?:\.\d+)?)K", model or "")
    return float(m.group(1)) if m else None

inv = get(f"/equipment/{SID}/list")  # confirm list endpoint
inverters = inv.get("reporters", {}).get("list") or get(f"/site/{SID}/inventory")["Inventory"]["inverters"]

end = datetime.now(timezone.utc).replace(microsecond=0)
start = end - timedelta(days=7)
fmt = "%Y-%m-%d %H:%M:%S"

units = []
for it in inverters:
    sn = it.get("SN") or it.get("serialNumber"); name = it.get("name")
    model = it.get("model","")
    d = get(f"/equipment/{SID}/{sn}/data", startTime=start.strftime(fmt), endTime=end.strftime(fmt))
    tel = d.get("data",{}).get("telemetries",[])
    by_day = defaultdict(list); last_mode=None; last_ts=None
    for s in tel:
        day = s["date"][:10]
        te = s.get("totalEnergy")
        if te is not None: by_day[day].append(te)
        last_mode = s.get("inverterMode"); last_ts = s["date"]
    daily = []
    for day in sorted(by_day):
        vals = by_day[day]
        kwh = round((max(vals)-min(vals))/1000.0, 2) if len(vals)>=2 else 0.0
        daily.append({"date": day, "kwh": kwh})
    # inverterMode: treat clear fault modes as error_code; STARTING/MPPT/PRODUCING are fine
    fault_modes = {"FAULT","ERROR","SHUTDOWN","LOCKED"}
    err = last_mode if (last_mode and last_mode.upper() in fault_modes) else None
    units.append({
        "id": name, "nameplate_kw": nameplate(model),
        "daily": daily, "error_code": err,
        "last_report": last_ts.replace(" ","T") if last_ts else None,
        "_model": model,
    })

print("=== UNITS BUILT FROM LIVE DATA ===")
for u in units:
    wk = sum(x["kwh"] for x in u["daily"])
    print(f"  {u['id']:12} {u['_model']:18} nameplate={u['nameplate_kw']}kW  7d={wk:.0f}kWh  last={u['last_report']}  err={u['error_code']}")

# strip private field before engine
for u in units: u.pop("_model")
result = analyze_cohort(units)

print("\n=== PEER ANALYSIS (Bruce's real cohort) ===")
print(f"cohort_size={result['cohort_size']}  degenerate={result['degenerate']}")
for u in result["units"]:
    pi = u.get("peer_index")
    pis = f"{pi:.2f}" if isinstance(pi,(int,float)) else "n/a"
    print(f"  {u['id']:12} status={u['status']:14} peer_index={pis:5}  window_kwh={u.get('window_kwh')}  {u.get('diagnosis','')}")
print("\nsummary:", json.dumps(result.get("summary",{})))
