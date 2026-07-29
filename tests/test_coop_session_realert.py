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


def _mk_tenant(*, capture_mode: str | None = "device") -> str:
    """Defaults to device mode: the one mode 'the extension will capture a
    fresh session' is actually true for. See test_capture_mode_gate below for
    the cloud/unset case, which must NOT get that instruction."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Coop Realert Test", contact_email=f"{tid}@t.t",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
            capture_mode=capture_mode,
        ))
        db.commit()
    return tid


def _mk_dead_coop_tenant(*, capture_mode: str | None = "device") -> tuple[str, int]:
    """A tenant whose VEC session died long enough ago to trigger the alert."""
    tid = _mk_tenant(capture_mode=capture_mode)
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


def test_cloud_tenant_gets_the_internal_alert_but_not_the_extension_email(monkeypatch):
    """Same bug as the GMP reauth flow, same fix: 'the extension will capture
    a fresh session' is not how Cloud Capture recovers a co-op login, so a
    cloud-mode tenant must not receive it. Ops still hears about the dead
    session either way — only the wrong customer instruction is withheld.

    (The email send is wrapped in a bare `except Exception` in scheduler.py,
    which would swallow an assertion raised from inside the mock — so this
    records calls instead of raising, and asserts on the recorded list.)"""
    notified = []
    emailed = []
    monkeypatch.setattr("api.notify.send_internal_alert",
                        lambda subject, body: notified.append(subject))
    monkeypatch.setattr("api.notify.send_coop_reauth_needed_email",
                        lambda **kw: emailed.append(kw) or True)
    tid, _arr = _mk_dead_coop_tenant(capture_mode="cloud")

    out = coop_session_death_warnings()

    assert [w for w in out["warned"] if w["tenant"] == tid] != []
    assert emailed == []  # the wrong instruction was withheld
    assert any(tid in s for s in notified)  # internal alert still fired


def test_cloud_tenant_without_a_credential_gets_the_connect_email(monkeypatch):
    """Confirmed real state on prod for all examined cloud-mode tenants:
    capture_mode='cloud' but no PortalCredential row at all — the honest ask
    is connecting Cloud Capture once, not the extension copy. (utility_name
    is whatever this fixture's 'vec' provider resolves to — not asserted
    here since the co-op name table is unrelated to this gate.)"""
    connect_calls = []
    monkeypatch.setattr("api.notify.send_internal_alert", lambda subject, body: None)
    monkeypatch.setattr("api.notify.send_coop_reauth_needed_email",
                        lambda **kw: (_ for _ in ()).throw(
                            AssertionError("must not send the extension-copy email")))
    monkeypatch.setattr("api.notify.send_connect_cloud_capture_email",
                        lambda **kw: connect_calls.append(kw) or True)
    tid, _arr = _mk_dead_coop_tenant(capture_mode="cloud")

    out = coop_session_death_warnings()

    assert [w for w in out["warned"] if w["tenant"] == tid] != []
    assert len(connect_calls) == 1
    assert connect_calls[0]["utility_name"]  # non-empty; exact string not our concern here
