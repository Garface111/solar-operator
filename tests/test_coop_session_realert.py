"""coop_session_death_warnings must re-alert a persistently-dead co-op session
every _COOP_REALERT_DAYS, not exactly once forever.

Ford, 2026-07-08: "find every instance of us intentionally sabotaging our own
reliability and fix it." This job alerted once on a co-op session death and
then NEVER again for that same incident, even if it stayed dead for weeks --
`_COOP_REALERT_DAYS` was declared but never actually used in the dedup check.
Fixed to match its sibling gmp_final_expiry_warnings' bounded-recurring shape:
re-alert at most every _COOP_REALERT_DAYS while still dead, never a tight
re-alert loop (the original 2026-07-06 flood this dedup was built to stop),
but never silent forever either.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta

from api.db import SessionLocal
from api.models import Array, Client, DailyGeneration, InverterAlertState, Tenant, UtilityAccount, UtilitySession
from api.scheduler import _COOP_REALERT_DAYS, _COOP_STALE_DAYS, coop_session_death_warnings


def _mk_tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Coop Realert Test", contact_email=f"{tid}@t.t",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
        ))
        db.commit()
    return tid


def _mk_dead_coop_tenant() -> tuple[str, int]:
    """A tenant whose VEC session died long enough ago to trigger the alert."""
    tid = _mk_tenant()
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="Dead Coop Client", active=True)
        db.add(c); db.flush()
        arr = Array(tenant_id=tid, client_id=c.id, name="Arr " + secrets.token_hex(3))
        db.add(arr); db.flush()
        db.add(UtilityAccount(
            tenant_id=tid, array_id=arr.id, provider="vec",
            account_number="acct-" + secrets.token_hex(3), enabled=True,
        ))
        stale_day = date.today() - timedelta(days=_COOP_STALE_DAYS + 5)
        db.add(DailyGeneration(tenant_id=tid, array_id=arr.id, day=stale_day,
                               kwh=12.5, source="utility_meter"))
        db.add(UtilitySession(
            tenant_id=tid, provider="vec", api_token="dead-token",
            captured_at=datetime.utcnow() - timedelta(days=_COOP_STALE_DAYS + 5),
        ))
        db.commit()
        return tid, arr.id


def test_dead_session_alerts_once_then_dedups_inside_the_realert_window(monkeypatch):
    sent = []
    monkeypatch.setattr("api.notify.send_internal_alert",
                        lambda subject, body: sent.append((subject, body)))
    monkeypatch.setattr("api.notify.send_coop_reauth_needed_email", lambda **kw: True)
    tid, _arr = _mk_dead_coop_tenant()

    out1 = coop_session_death_warnings()
    mine1 = [w for w in out1["warned"] if w["tenant"] == tid]
    assert len(mine1) == 1
    mine_sent = [s for s in sent if tid in s[1]]
    assert len(mine_sent) == 1

    # Still dead, still inside the re-alert window -> deduped, no repeat email.
    sent.clear()
    out2 = coop_session_death_warnings()
    assert [w for w in out2["warned"] if w["tenant"] == tid] == []
    assert [s for s in sent if tid in s[1]] == []


def test_dead_session_re_alerts_after_the_realert_window_passes(monkeypatch):
    """The actual fix: a persistently-dead session eventually alerts AGAIN
    instead of staying silent forever after the first email."""
    sent = []
    monkeypatch.setattr("api.notify.send_internal_alert",
                        lambda subject, body: sent.append((subject, body)))
    monkeypatch.setattr("api.notify.send_coop_reauth_needed_email", lambda **kw: True)
    tid, arr_id = _mk_dead_coop_tenant()

    coop_session_death_warnings()  # first alert, records last_alerted_at = now

    # Backdate the incident's last_alerted_at past the re-alert window, as if
    # it had been dead and silent for that long.
    with SessionLocal() as db:
        from sqlalchemy import select
        state = db.execute(select(InverterAlertState).where(
            InverterAlertState.tenant_id == tid,
            InverterAlertState.incident_key == f"coop_session_dead:{tid}:vec",
        )).scalar_one()
        state.last_alerted_at = datetime.utcnow() - timedelta(days=_COOP_REALERT_DAYS + 1)
        db.commit()

    sent.clear()
    out = coop_session_death_warnings()
    assert [w for w in out["warned"] if w["tenant"] == tid] != []
    assert [s for s in sent if tid in s[1]] != []
