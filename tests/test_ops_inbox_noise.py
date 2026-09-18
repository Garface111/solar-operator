"""Regressions for the 2026-09-18 ops-inbox sweep.

Ford's Dyson Swarm inbox held 3,136 internal alerts from arrayoperator.com over
60 days. Four mechanisms produced nearly all of them, and each is covered here:

  * 882  CRITICAL "portal vault decrypt on public process" -- the harvester, the
         one process ALLOWED to unwrap, never named itself, so every legitimate
         capture reported role=unknown and paged.
  * 176  LockNotAvailable 500s on /v1/sync + /v1/array-owners/utility-meter-capture
         -- a last_seen recency stamp fighting a long ingest for the same rows.
  *  55  "undeliverable_blocked: NXDOMAIN" for a seeded demo fixture that can
         never receive mail, in an alert whose body contradicted itself.
  *  the flood shape itself -- every throttle was process-local, so short-lived
         processes reset it and it capped nothing.
"""
from __future__ import annotations

import os
import secrets

import pytest
from sqlalchemy import select

from api.db import SessionLocal, flush_last_seen_stamps
from api.models import Tenant, UtilityAccount, now


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="LS Test", contact_email=f"{tid}@t.test",
                      tenant_key="k_" + secrets.token_hex(8), plan="standard",
                      active=True, product="array_operator"))
        db.commit()
    return tid


def _account(tid: str, *, last_seen=None) -> int:
    with SessionLocal() as db:
        ua = UtilityAccount(tenant_id=tid, provider="gmp",
                            account_number=secrets.token_hex(5),
                            last_seen=last_seen or now())
        db.add(ua)
        db.commit()
        return ua.id


# ── deferred last_seen ──────────────────────────────────────────────────────

def test_touch_last_seen_does_not_dirty_the_row():
    """The whole point: a stale stamp must NOT enrol the row in the caller's
    transaction, because that transaction is a long ingest holding the lock."""
    import datetime as dt
    tid = _tenant()
    aid = _account(tid, last_seen=now() - dt.timedelta(days=2))
    with SessionLocal() as db:
        ua = db.get(UtilityAccount, aid)
        assert ua.touch_last_seen() is True          # stale -> scheduled
        assert ua not in db.dirty, "last_seen joined the caller's transaction"
        assert db.info["_last_seen_pending"] == {aid}


def test_touch_last_seen_skips_a_fresh_row():
    tid = _tenant()
    aid = _account(tid)                              # last_seen = now
    with SessionLocal() as db:
        ua = db.get(UtilityAccount, aid)
        assert ua.touch_last_seen() is False
        assert "_last_seen_pending" not in db.info


def test_commit_stamps_the_pending_ids():
    import datetime as dt
    tid = _tenant()
    old = now() - dt.timedelta(days=2)
    aid = _account(tid, last_seen=old)
    with SessionLocal() as db:
        db.get(UtilityAccount, aid).touch_last_seen()
        db.commit()                                   # after_commit hook fires
    with SessionLocal() as db:
        assert db.get(UtilityAccount, aid).last_seen > old


def test_rollback_drops_the_pending_stamp():
    """The ingest that wanted the stamp did not land -- don't claim we saw it."""
    import datetime as dt
    tid = _tenant()
    old = now() - dt.timedelta(days=2)
    aid = _account(tid, last_seen=old)
    with SessionLocal() as db:
        db.get(UtilityAccount, aid).touch_last_seen()
        db.rollback()
        assert "_last_seen_pending" not in db.info
    with SessionLocal() as db:
        assert db.get(UtilityAccount, aid).last_seen == old


def test_flush_never_raises_on_garbage():
    """Contention, a dead pool, a vanished row: all must be survivable. A
    recency marker is never worth failing a customer's sync for."""
    assert flush_last_seen_stamps([]) == 0
    assert flush_last_seen_stamps(None) == 0
    assert flush_last_seen_stamps([10**12]) == 0      # no such row


# ── placeholder recipients ──────────────────────────────────────────────────

@pytest.mark.parametrize("addr", [
    "demo-realistic@energyagent-demo.com",
    "deleted+ten_60309c3a@invalid.local",
    "someone@example.com",
    "x@test.com",
])
def test_placeholder_recipients_are_recognised(addr):
    from api.email_archive import is_placeholder_recipient
    assert is_placeholder_recipient(addr) is True


@pytest.mark.parametrize("addr", [
    "ford.genereaux@dysonswarmtechnologies.com",
    "bruce.genereaux@gmail.com",
    "billing@arrayoperator.com",
])
def test_real_recipients_are_not_placeholders(addr):
    from api.email_archive import is_placeholder_recipient
    assert is_placeholder_recipient(addr) is False


def test_blocked_fixture_is_archived_but_not_alerted(monkeypatch):
    from api import email_archive as ea
    alerted: list = []
    monkeypatch.setattr(ea, "_alert", lambda *a, **k: alerted.append(a))
    ea.record_blocked("demo-realistic@energyagent-demo.com",
                      "Fleet digest held", "domain does not exist (NXDOMAIN)",
                      source="test")
    assert alerted == []
    with SessionLocal() as db:
        row = db.execute(
            select(ea.EmailArchive)
            .where(ea.EmailArchive.to_email == "demo-realistic@energyagent-demo.com")
            .order_by(ea.EmailArchive.id.desc())
        ).scalars().first()
    assert row is not None, "the block must still be archived for the audit trail"
    assert "undeliverable_blocked" in (row.flags or "")


def test_blocked_real_address_still_alerts(monkeypatch):
    from api import email_archive as ea
    alerted: list = []
    monkeypatch.setattr(ea, "_alert", lambda *a, **k: alerted.append(k))
    ea.record_blocked("someone@a-real-domain-that-bounced.com", "Sign-in link",
                      "domain publishes a null MX (RFC 7505)", source="test")
    assert len(alerted) == 1
    assert alerted[0].get("blocked") is True


def test_blocked_alert_body_does_not_contradict_itself(monkeypatch):
    """It opened with 'NOT SENT' and closed with 'The email WAS sent'."""
    from api import email_archive as ea
    import api.notify as notify
    sent: list = []
    monkeypatch.setattr(notify, "send_internal_alert",
                        lambda s, b, **k: sent.append((s, b)) or True)
    monkeypatch.setattr(ea, "_recent_alert_sent", lambda *a, **k: False)
    row = ea.EmailArchive(to_email="x@y.test", subject="s",
                          body_text="NOT SENT - pre-flight blocked.")
    with SessionLocal() as db:
        ea._alert(db, row, ["undeliverable_blocked:NXDOMAIN"], blocked=True)
    assert sent, "alert did not fire"
    body = sent[0][1]
    assert "was NOT sent" in body
    assert "WAS sent" not in body


# ── vault decrypt: name the process, don't page about it ────────────────────

def test_vault_decrypt_on_unnamed_process_does_not_page(monkeypatch, caplog):
    """The harvester is ARMED on purpose. Before Dockerfile.harvester named it,
    role=unknown meant 882 CRITICAL pages for the system working correctly."""
    import logging
    from api import crypto
    import api.notify as notify

    from cryptography.fernet import Fernet
    KEY = Fernet.generate_key().decode()
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    monkeypatch.setenv("SO_VAULT_DECRYPT", "1")
    monkeypatch.delenv("PROCESS_ROLE", raising=False)
    crypto._cache.clear(); crypto._vol_counts.clear()
    crypto._alert_last_sent.clear(); crypto._vol_window_start = 0.0

    sent: list = []
    monkeypatch.setattr(notify, "send_internal_alert",
                        lambda s, b, **k: sent.append((s, b)))

    ct = crypto.encrypt_str("portal-pw")
    with caplog.at_level(logging.WARNING, logger="solar.crypto"):
        assert crypto.decrypt_vault_str(ct) == "portal-pw"

    assert sent == [], f"unnamed armed process paged: {sent}"
    assert any("unnamed_process" in r.message for r in caplog.records), \
        "the naming gap must still be visible in the log"


def test_harvester_image_names_its_process_role():
    """crypto._note_decrypt can only tell a harvester from a leaked public
    process by PROCESS_ROLE. The image has to set it."""
    import pathlib
    df = pathlib.Path(__file__).resolve().parents[1] / "Dockerfile.harvester"
    assert "PROCESS_ROLE=cloud-capture-harvester" in df.read_text(encoding="utf-8")


def test_alert_cooldown_does_not_swallow_the_first_page(monkeypatch):
    """time.monotonic() counts from BOOT, so the old 0.0 'never sent' sentinel
    silenced every first alert for the first hour of machine uptime."""
    from api import crypto
    import api.notify as notify
    sent: list = []
    monkeypatch.setattr(notify, "send_internal_alert",
                        lambda s, b, **k: sent.append((s, b)))
    crypto._alert_last_sent.clear()
    crypto._maybe_alert(alert_key="k1", subject="first", body="b", now_m=1.0)
    assert len(sent) == 1, "the first alert was swallowed"
    crypto._maybe_alert(alert_key="k1", subject="first", body="b", now_m=2.0)
    assert len(sent) == 1, "the repeat was not throttled"


# ── the flood shape: a durable, cross-process throttle ──────────────────────

def test_identical_alerts_are_deduped_across_processes(monkeypatch):
    """The throttle must live in the archive, not in a module global -- that is
    exactly what 882 short-lived harvester processes walked straight past."""
    import api.notify as notify
    from api import email_archive as ea

    monkeypatch.setattr(notify, "INTERNAL_ALERT_TO", "ops@dysonswarmtechnologies.com")
    monkeypatch.setattr(ea, "is_internal", lambda *a, **k: True)
    sends: list = []
    monkeypatch.setattr(notify, "_send_via_resend",
                        lambda **kw: (sends.append(kw), True)[1])

    subject = "CRITICAL: portal vault decrypt on public process"
    body = "kind=vault role=unknown count_in_window=1."

    assert notify.send_internal_alert(subject, body) is True
    assert len(sends) == 1
    # Archive what the first send would have recorded, as the choke point does.
    with SessionLocal() as db:
        db.add(ea.EmailArchive(to_email="ops@dysonswarmtechnologies.com",
                               subject=sends[0]["subject"], body_text=body))
        db.commit()

    # A fresh process cannot see the in-memory cooldown -- simulate that.
    notify._suppressed_since_send.clear()
    assert notify.send_internal_alert(subject, body) is False
    assert len(sends) == 1, "the repeat escaped the durable throttle"


def test_a_different_tenant_is_not_collapsed_into_one_alert(monkeypatch):
    """Dedupe must key on the CONTENT, so two real tenants both get through."""
    import api.notify as notify
    key_a = notify._alert_dedupe_key("Trial paused (no card)", "tenant ten_aaa11111")
    key_b = notify._alert_dedupe_key("Trial paused (no card)", "tenant ten_bbb22222")
    key_a2 = notify._alert_dedupe_key("Trial paused (no card)", "tenant ten_aaa11111")
    assert key_a == key_a2
    assert key_a != key_b


def test_internal_alert_wears_the_right_brand():
    """The product is Array Operator; alerts arrived stamped [NEPOOL Operator]."""
    import api.notify as notify
    assert notify.ALERT_SUBJECT_PREFIX == "Array Operator"
