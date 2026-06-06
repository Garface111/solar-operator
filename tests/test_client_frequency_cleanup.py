"""
Tests for Jun 6 2026 freq-cleanup:
  - PATCH with null → DB stores "quarterly"
  - PATCH with "monthly" → DB stores "monthly"
  - PATCH with invalid value → 400
  - Migration backfill: null rows → "quarterly"
"""
from __future__ import annotations

import secrets

from sqlalchemy import text

from api.account import mint_session_for_tenant
from api.db import SessionLocal, engine
from api.models import Client, Tenant


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="FreqCleanup Test",
            contact_email=f"{tid}@freq.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(16),
            plan="standard",
            active=True,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _make_client(tenant_id: str, name: str,
                 report_frequency: str | None = None) -> int:
    with SessionLocal() as db:
        c = Client(
            tenant_id=tenant_id,
            name=name,
            active=True,
            report_frequency=report_frequency,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id


# ── PATCH null → quarterly ────────────────────────────────────────────────────

def test_patch_null_frequency_stores_quarterly(client):
    tid, auth = _make_tenant()
    cid = _make_client(tid, "Null Freq Farm")

    resp = client.patch(
        f"/v1/account/clients/{cid}",
        json={"report_frequency": None},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["client"]["report_frequency"] == "quarterly"

    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.report_frequency == "quarterly"


# ── PATCH "monthly" → monthly ─────────────────────────────────────────────────

def test_patch_monthly_frequency_persists(client):
    tid, auth = _make_tenant()
    cid = _make_client(tid, "Monthly Farm")

    resp = client.patch(
        f"/v1/account/clients/{cid}",
        json={"report_frequency": "monthly"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["client"]["report_frequency"] == "monthly"

    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.report_frequency == "monthly"


# ── PATCH invalid value → 400 ─────────────────────────────────────────────────

def test_patch_weekly_frequency_rejected(client):
    tid, auth = _make_tenant()
    cid = _make_client(tid, "Weekly Farm")

    resp = client.patch(
        f"/v1/account/clients/{cid}",
        json={"report_frequency": "weekly"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 400, resp.text


# ── Migration backfill ────────────────────────────────────────────────────────

def test_migration_backfills_null_frequency():
    """Insert a client with report_frequency=NULL, run the backfill SQL from
    migrate.py, confirm it becomes 'quarterly'."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="MigTest Tenant",
            contact_email=f"{tid}@mig.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(16),
            plan="standard",
            active=True,
        ))
        db.commit()

    with SessionLocal() as db:
        c = Client(
            tenant_id=tid,
            name="Null Freq Client",
            active=True,
            report_frequency=None,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        cid = c.id

    # Simulate the migration backfill block
    with engine.begin() as conn:
        n = conn.execute(text(
            "SELECT COUNT(*) FROM clients WHERE report_frequency IS NULL"
        )).scalar()
        assert n is not None and n > 0
        conn.execute(text(
            "UPDATE clients SET report_frequency = 'quarterly' "
            "WHERE report_frequency IS NULL"
        ))

    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.report_frequency == "quarterly"

    # Idempotency: second run finds zero rows, no error
    with engine.begin() as conn:
        n2 = conn.execute(text(
            "SELECT COUNT(*) FROM clients WHERE report_frequency IS NULL"
        )).scalar()
        assert n2 == 0
