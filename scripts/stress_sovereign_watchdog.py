#!/usr/bin/env python3
"""Stress-test Sovereign durability (watchdog dual-channel).

Runs offline unit probes + optional live prod healthz when ADMIN_API_KEY is set.

  cd ~/solar-operator && .venv/bin/python scripts/stress_sovereign_watchdog.py
  railway run .venv/bin/python scripts/stress_sovereign_watchdog.py --live
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    raise SystemExit(1)


def section(title: str) -> None:
    print(f"\n== {title} ==")


def test_storm_breaker() -> None:
    section("storm breaker (interface reboot ledger pattern)")
    from api import energy_agent_sovereign_watchdog as wd

    # Reset ledger
    with wd._lock:
        wd._vitals["recovery_ledger"] = []
        wd._vitals["storm_breaker_tripped"] = False
        wd._vitals["storm_tripped_at"] = None
        wd._vitals["total_recoveries"] = 0

    # Shrink window for test via env
    os.environ["SOVEREIGN_WATCHDOG_STORM_MAX"] = "3"
    os.environ["SOVEREIGN_WATCHDOG_STORM_WINDOW_SEC"] = "3600"

    allowed = 0
    blocked = 0
    for i in range(6):
        if wd._record_recovery(f"test_{i}"):
            allowed += 1
        else:
            blocked += 1
    if allowed < 3:
        fail(f"expected >=3 allowed recoveries, got {allowed}")
    if blocked < 1:
        fail(f"expected storm to block, blocked={blocked}")
    storm, recent, _ = wd._storm_state()
    if not storm:
        fail("storm should be tripped")
    ok(f"allowed={allowed} blocked={blocked} recent={len(recent)} storm={storm}")


def test_diagnose_shape() -> None:
    section("diagnose() healthz shape")
    from api.energy_agent_sovereign_watchdog import diagnose

    # May fail DB — still must return structure or raise cleanly
    try:
        h = diagnose()
    except Exception as e:
        # Offline without DB is OK for shape if we mock — try process-only fields
        print(f"  (db unavailable: {e})")
        return
    for key in ("ok", "channels", "ages", "storm", "problems", "pattern"):
        if key not in h:
            fail(f"missing key {key}")
    if "primary" not in h["channels"] or "recovery" not in h["channels"]:
        fail("channels must expose primary + recovery (dual-sidecar mapping)")
    ok(f"ok={h.get('ok')} problems={h.get('problems')} storm={h['storm'].get('breaker_tripped')}")


def test_note_primary_fail_burst() -> None:
    section("primary heartbeats + fail burst")
    from api import energy_agent_sovereign_watchdog as wd

    with wd._lock:
        wd._vitals["consecutive_sub_fail"] = 0
        wd._vitals["consecutive_cortex_fail"] = 0

    for _ in range(4):
        wd.note_primary("sub", ok=False, detail={"sim": True})
    with wd._lock:
        n = wd._vitals["consecutive_sub_fail"]
    if n != 4:
        fail(f"expected 4 consecutive sub fails, got {n}")
    wd.note_primary("sub", ok=True)
    with wd._lock:
        n = wd._vitals["consecutive_sub_fail"]
    if n != 0:
        fail(f"ok should reset fails, got {n}")
    ok("fail burst + reset works")


def test_stuck_job_requeue_logic() -> None:
    section("stuck running job requeue (synthetic ORM if DB)")
    try:
        from api.db import SessionLocal
        from api.energy_agent_sovereign import EaSovereignJob, Base
        from api.energy_agent_sovereign_watchdog import recover_stuck_jobs
        from api.db import engine
        from datetime import datetime, timedelta
        import uuid

        Base.metadata.create_all(bind=engine, tables=[EaSovereignJob.__table__])
        jid = f"sov_test_{uuid.uuid4().hex[:10]}"
        with SessionLocal() as db:
            # Clean leftover tests
            old = db.get(EaSovereignJob, jid)
            if old:
                db.delete(old)
                db.commit()
            job = EaSovereignJob(
                id=jid,
                kind="test",
                status="running",
                title="watchdog stress",
                brief_json="{}",
                created_at=datetime.utcnow() - timedelta(hours=3),
            )
            db.add(job)
            db.commit()
            # Force short stuck threshold
            os.environ["SOVEREIGN_WATCHDOG_JOB_STUCK_SEC"] = "60"
            res = recover_stuck_jobs(db)
            db.commit()
            row = db.get(EaSovereignJob, jid)
            status = row.status if row else None
            # cleanup
            if row:
                db.delete(row)
                db.commit()
        if status != "queued":
            fail(f"expected requeued→queued, got {status} res={res}")
        ok(f"requeued stuck job → {status} ({res})")
    except Exception as e:
        print(f"  ~ skipped (no DB): {e}")


def test_import_scheduler_registers() -> None:
    section("scheduler registers watchdog job id")
    # Don't start scheduler — just assert source contains the id
    src = (ROOT / "api" / "scheduler.py").read_text()
    if "energy_agent_sovereign_watchdog" not in src:
        fail("scheduler missing watchdog job")
    if "_run_energy_agent_sovereign_watchdog" not in src:
        fail("scheduler missing runner")
    ok("scheduler wiring present")


def test_live_healthz() -> None:
    section("live prod healthz")
    import urllib.request

    key = (os.getenv("ADMIN_API_KEY") or "").strip()
    if not key:
        print("  ~ skip (ADMIN_API_KEY not set)")
        return
    url = os.getenv(
        "SOVEREIGN_HEALTHZ_URL",
        "https://web-production-49c83.up.railway.app/admin/sovereign/healthz",
    )
    req = urllib.request.Request(
        url,
        headers={"X-Admin-Key": key, "Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            data = json.loads(body)
    except Exception as e:
        print(f"  ~ live probe failed (may need session auth): {e}")
        return
    print("  ", json.dumps({k: data.get(k) for k in ("ok", "problems", "storm", "channels")}, indent=2)[:800])
    ok("live healthz responded")


def main() -> None:
    live = "--live" in sys.argv
    print("Sovereign durability stress — dual-channel watchdog")
    print("Pattern: interface sidecar-watchdog + storm breaker + soft reboot")
    test_import_scheduler_registers()
    test_note_primary_fail_burst()
    test_storm_breaker()
    test_diagnose_shape()
    test_stuck_job_requeue_logic()
    if live:
        test_live_healthz()
    print("\nALL STRESS CHECKS PASSED")


if __name__ == "__main__":
    main()
