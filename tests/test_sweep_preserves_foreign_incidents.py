"""Regression: the inverter alert sweep must NOT delete incident rows owned by
OTHER alert jobs that share inverter_alert_state (coop_session_dead:*,
gmp_token_final:*). Deleting them reset those jobs' dedup every sweep and made
the co-op-DEAD / GMP-token FINAL WARNING alerts re-fire on a loop.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from api.models import Base, Tenant, InverterAlertState
from api import inverter_alert_sweep as sweep


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_sweep_clears_recovered_inverters_but_keeps_foreign_incidents(db, monkeypatch):
    t = Tenant(id="ten_x", name="X", contact_email="x@x.com", tenant_key="k",
               product="array_operator", billing_plan="both")
    # enable the sweep for this tenant
    for attr, val in (("inverter_alerts_enabled", True),
                      ("inverter_alert_grace_hours", 0),
                      ("inverter_alert_threshold_pct", 50)):
        try: setattr(t, attr, val)
        except Exception: pass
    db.add(t)
    old = datetime.utcnow() - timedelta(days=5)
    # a recovered inverter incident (no longer flagged) -> should be DELETED
    db.add(InverterAlertState(tenant_id="ten_x", incident_key="100|5",
                              first_flagged_at=old, last_alerted_at=old))
    # foreign incidents owned by other jobs -> must SURVIVE
    db.add(InverterAlertState(tenant_id="ten_x",
                              incident_key="coop_session_dead:ten_x:vec",
                              first_flagged_at=old, last_alerted_at=old))
    db.add(InverterAlertState(tenant_id="ten_x",
                              incident_key="gmp_token_final:ten_x",
                              first_flagged_at=old, last_alerted_at=old))
    db.commit()

    # No inverters flagged this sweep (empty fleet tree).
    monkeypatch.setattr(sweep, "inverter_fleet",
                        type("M", (), {"build_fleet_tree": staticmethod(lambda *a, **k: {})}))
    # ao_gets_vendor_emails gate -> allow
    monkeypatch.setattr(sweep, "ao_gets_vendor_emails", lambda *a, **k: True)

    sweep.sweep_tenant(db, t)
    db.commit()

    keys = {r.incident_key for r in db.execute(select(InverterAlertState)).scalars()}
    assert "100|5" not in keys, "recovered inverter incident should be cleared"
    assert "coop_session_dead:ten_x:vec" in keys, "co-op incident must survive"
    assert "gmp_token_final:ten_x" in keys, "gmp-token incident must survive"
