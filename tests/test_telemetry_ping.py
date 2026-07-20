"""session_ping / time-on-site telemetry."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select, func

from api.account import _sign_session
from api.app import app
from api.db import SessionLocal
from api.models import SessionPing, Tenant


def _mk_tenant(db, tid: str = "ten_tel_test_01") -> Tenant:
    t = db.get(Tenant, tid)
    if t:
        return t
    t = Tenant(
        id=tid,
        name="Telemetry Test",
        company_name="Telemetry Co",
        contact_email=f"{tid}@example.com",
        tenant_key=f"sol_live_telemetry_{tid}",
        plan="comped",
        product="array_operator",
        active=True,
        is_demo=False,
    )
    db.add(t)
    db.commit()
    return t


def test_ping_creates_one_row_per_minute():
    client = TestClient(app)
    with SessionLocal() as db:
        t = _mk_tenant(db)
        # clean prior pings
        for r in db.execute(select(SessionPing).where(SessionPing.tenant_id == t.id)).scalars():
            db.delete(r)
        db.commit()
        token = _sign_session(str(t.id))

    r1 = client.post(
        "/v1/telemetry/ping",
        headers={"Authorization": f"Bearer {token}"},
        json={"path": "/#arrays"},
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["ok"] is True
    assert body1["created"] is True

    r2 = client.post(
        "/v1/telemetry/ping",
        headers={"Authorization": f"Bearer {token}"},
        json={"path": "/#account"},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["created"] is False  # same minute bucket
    assert body2["minute_bucket"] == body1["minute_bucket"]

    with SessionLocal() as db:
        n = db.execute(
            select(func.count()).select_from(SessionPing).where(
                SessionPing.tenant_id == "ten_tel_test_01"
            )
        ).scalar()
        assert n == 1
        row = db.execute(
            select(SessionPing).where(SessionPing.tenant_id == "ten_tel_test_01")
        ).scalar_one()
        assert row.path == "/#account"  # updated on second ping


def test_ping_requires_auth():
    client = TestClient(app)
    r = client.post("/v1/telemetry/ping", json={"path": "/"})
    assert r.status_code in (401, 403)


def test_time_on_site_is_distinct_minutes():
    """Bridge formula: count distinct minute_buckets."""
    with SessionLocal() as db:
        t = _mk_tenant(db, "ten_tel_test_02")
        for r in db.execute(select(SessionPing).where(SessionPing.tenant_id == t.id)).scalars():
            db.delete(r)
        db.commit()
        base = datetime.utcnow().replace(second=0, microsecond=0)
        for i in range(5):
            bucket = base - timedelta(minutes=i)
            db.add(SessionPing(
                tenant_id=t.id,
                email=t.contact_email,
                day=bucket.date(),
                minute_bucket=bucket,
                path="/",
            ))
        db.commit()
        minutes = db.execute(
            select(func.count(func.distinct(SessionPing.minute_bucket))).where(
                SessionPing.tenant_id == t.id
            )
        ).scalar()
        assert minutes == 5
