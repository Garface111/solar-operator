"""One-shot migration: export current PROVIDERS + SmartHub hosts into per-state CSVs.

Run once with the SO venv active. Writes api/data/providers/<state>.csv.
After this, providers.py loads FROM these CSVs (the data files become the
single source of truth) and the script can be deleted.
"""
import csv
import pathlib
import importlib.util

ROOT = pathlib.Path(__file__).resolve().parents[1]  # ~/solar-operator


def _load(modpath, name):
    spec = importlib.util.spec_from_file_location(name, modpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load providers.py and smarthub.py WITHOUT importing the adapters package
# (which drags in pdfplumber via gmp). They have no heavy deps themselves.
providers = _load(ROOT / "api" / "providers.py", "so_providers")
smarthub = _load(ROOT / "api" / "adapters" / "smarthub.py", "so_smarthub")

PROVIDERS = providers.PROVIDERS
code_to_host = {info["provider"]: info["host"] for info in smarthub.SMARTHUB_UTILITIES.values()}

COLS = ["code", "label", "state", "scrape_status", "smarthub_host", "portal_url", "notes"]


def region_file(p):
    code = p["code"]
    if code in ("other", "vpps"):
        return "_core"
    st = (p["state"] or "").strip().upper()
    return st if st else "_core"


buckets = {}
for p in PROVIDERS:
    rf = region_file(p)
    buckets.setdefault(rf, []).append({
        "code": p["code"],
        "label": p["label"],
        "state": p["state"],
        "scrape_status": p["scrape_status"],
        "smarthub_host": code_to_host.get(p["code"], ""),
        "portal_url": p["portal_url"],
        "notes": p["notes"],
    })

out_dir = ROOT / "api" / "data" / "providers"
out_dir.mkdir(parents=True, exist_ok=True)

for rf, rows in sorted(buckets.items()):
    rows.sort(key=lambda r: (r["scrape_status"] != "live", r["code"]))
    path = out_dir / f"{rf}.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"wrote {path.relative_to(ROOT)}  ({len(rows)} rows)")

print("TOTAL providers migrated:", sum(len(v) for v in buckets.values()))
