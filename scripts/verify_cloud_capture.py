"""Offline verification for Cloud Capture (no live portals).

Checks:
  1. The whole API imports (router wiring doesn't break boot) and create_all
     builds the two new tables.
  2. Credential vault round-trip: with SO_CONFIG_KEY set, a password stored via
     upsert_credential is ciphertext at rest (SOENC1:) and decrypts back exactly.
  3. Pure reducers: SmartHub negative-y generation reduction + bill-row shaping.
  4. Vendor registry routing (gmp / co-op / inverter).
Run: ~/hv/bin/python scripts/verify_cloud_capture.py
"""
import os
import sys
import tempfile

# Point at a throwaway sqlite DB and arm encryption BEFORE importing the app.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/verify.db"
os.environ.setdefault("SO_CONFIG_KEY", "")  # set below after we can import Fernet

from cryptography.fernet import Fernet  # noqa: E402
os.environ["SO_CONFIG_KEY"] = Fernet.generate_key().decode()

FAILS = []
def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + extra) if extra else ''}")
    if not cond:
        FAILS.append(name)

print("1) App import + create_all")
from api import app as app_mod            # noqa: E402  (imports the whole router graph)
from api.db import SessionLocal, engine   # noqa: E402
from api.models import Base, PortalCredential, HarvestRun, Tenant, now  # noqa: E402
Base.metadata.create_all(bind=engine)
tables = set(Base.metadata.tables)
check("api.app imports (router wiring OK)", hasattr(app_mod, "app"))
check("portal_credential table defined", "portal_credential" in tables)
check("harvest_run table defined", "harvest_run" in tables)
# Confirm the cloud-capture routes are registered on the app.
paths = {r.path for r in app_mod.app.routes if hasattr(r, "path")}
check("POST /v1/cloud-capture/credentials registered", "/v1/cloud-capture/credentials" in paths)
check("GET /v1/cloud-capture/status registered", "/v1/cloud-capture/status" in paths)

print("2) Credential vault encrypt/decrypt round-trip")
from api.harvester import credentials as cc  # noqa: E402
check("crypto armed (SO_CONFIG_KEY set)", cc.crypto_ready())
with SessionLocal() as db:
    db.add(Tenant(id="ten_verify", name="Verify", tenant_key="sol_live_verify",
                  contact_email="verify@example.com"))
    db.commit()
    row = cc.upsert_credential(db, "ten_verify", "gmp", "owner@example.com",
                               "s3cr3t-p@ss", enable=True)
    db.commit()
    rid = row.id
# Read the RAW column value straight from SQL to prove it's ciphertext at rest.
from sqlalchemy import text  # noqa: E402
with engine.connect() as conn:
    raw = conn.execute(text("SELECT secret_enc FROM portal_credential WHERE id=:i"),
                       {"i": rid}).scalar()
check("password stored as ciphertext (SOENC1:)", isinstance(raw, str) and raw.startswith("SOENC1:"),
      f"raw={str(raw)[:24]}…")
check("password does NOT appear in plaintext at rest", "s3cr3t" not in (raw or ""))
with SessionLocal() as db:
    got = db.get(PortalCredential, rid)
    creds = cc.load_creds(got)
check("decrypts back to the exact password", creds is not None and creds.password == "s3cr3t-p@ss")

print("3) Pure reducers")
from api.harvester.vendors.smarthub import SmartHubVendor  # noqa: E402
out = {}
# NISC usage: negative y = export = generation; positive/zero ignored.
SmartHubVendor._reduce({"ELECTRIC": [{"series": [{"data": [
    {"x": 1704067200000, "y": -12.5},   # 2024-01-01, 12.5 kWh exported
    {"x": 1704153600000, "y":  3.0},    # consumption — ignored
    {"x": 1704240000000, "y": -8.0},    # export
]}]}]}, out)
check("negative-y summed as generation", out.get("2024-01-01") == 12.5 and out.get("2024-01-03") == 8.0,
      f"out={out}")
check("positive-y (consumption) ignored", "2024-01-02" not in out)
bill = SmartHubVendor._bill_row("6578300", {
    "acctNbr": "6578300", "billingDate": "06/01/2026", "adjustedBillAmount": 142.5,
    "totalUsage": 0, "billProcessUuid": "abc-uuid", "systemOfRecord": "NISC",
    "servLocs": [{"id": {"srvLocNbr": "65783"}, "address": {"addr1": "52 County Rd", "city": "Glover"}}],
})
check("bill row maps uuid + amount + account", bill["bill_uuid"] == "abc-uuid"
      and bill["bill_amount"] == 142.5 and bill["account_id"] == "6578300")

print("4) Vendor registry routing")
from api.harvester.vendors import module_for  # noqa: E402
check("gmp -> GMPVendor", module_for("gmp").__class__.__name__ == "GMPVendor")
check("vec -> SmartHubVendor", module_for("vec").__class__.__name__ == "SmartHubVendor")
check("sh_glover -> SmartHubVendor", module_for("sh_glover").__class__.__name__ == "SmartHubVendor")
check("fronius -> FroniusVendor", module_for("fronius").__class__.__name__ == "FroniusVendor")
check("sma -> SMAVendor", module_for("sma").__class__.__name__ == "SMAVendor")
check("chint -> ChintVendor", module_for("chint").__class__.__name__ == "ChintVendor")
check("solaredge -> None (server-side API poll)", module_for("solaredge") is None)

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
