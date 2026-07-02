"""
Trial-end reminder dedup, gated by per-stage timestamps.

The old logic deduped on a 1-day rolling window (now+2d < trial_ends_at <=
now+3d), which dropped or duplicated the email if the daily tick was missed or
fired twice. The current logic (commit 06f499ea) is a TWO-TOUCH card-capture
nudge for no-card trialing tenants:
  EARLY  ~7 days out -> stamped by trial_reminder_sent_at
  URGENT ~2 days out -> stamped by trial_final_reminder_sent_at (after EARLY)
Each stage is gated by its own NULL-check + successful-send stamp, so a missed
or double-fired daily tick never drops or duplicates a stage.

This test pins the EARLY stage's exactly-once behavior: a tenant ~5 days out
(inside the 7-day EARLY window, OUTSIDE the 2-day URGENT window) must receive
the EARLY reminder exactly once and never re-fire on a later tick. The 5-day
placement is deliberate — a <=2-day tenant would also (correctly) qualify for
the distinct URGENT touch, which is a separate exactly-once path, not a dup.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from api.db import SessionLocal
from api.models import Tenant, now
import api.scheduler as scheduler


def _make_trialing_no_card(days_to_end: int = 2) -> tuple[str, str]:
    tid = f"ten_rem_{secrets.token_hex(4)}"
    email = f"{tid}@rem.test"
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Reminder Co", company_name="Reminder Co",
            contact_email=email,
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard", active=True, subscription_status="trialing",
            stripe_payment_method_id=None,
            trial_ends_at=now() + timedelta(days=days_to_end),
            created_at=now(),
        ))
        db.commit()
    return tid, email


def test_reminder_fires_once_then_not_again(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    # 5 days out: inside the 7-day EARLY window, outside the 2-day URGENT window,
    # so this exercises the EARLY stage's exactly-once path in isolation.
    tid, email = _make_trialing_no_card(days_to_end=5)

    # Count only sends addressed to OUR tenant — the shared test DB may hold
    # other trialing tenants the scheduler also (legitimately) reminds.
    my_calls: list[dict] = []

    def fake_send(**kwargs):
        if kwargs.get("to") == email:
            my_calls.append(kwargs)
        return True

    monkeypatch.setattr(
        scheduler, "send_trial_ending_no_card_reminder_email", fake_send)

    r1 = scheduler.send_trial_ending_reminders()
    assert tid in r1["reminded"]
    assert len(my_calls) == 1

    with SessionLocal() as db:
        assert db.get(Tenant, tid).trial_reminder_sent_at is not None

    # Second tick: already stamped → no re-fire, no duplicate email.
    r2 = scheduler.send_trial_ending_reminders()
    assert tid not in r2["reminded"]
    assert len(my_calls) == 1


def test_reminder_not_stamped_on_send_failure(monkeypatch):
    """A failed send leaves trial_reminder_sent_at NULL so the next tick retries
    (at-least-once on failure, exactly-once on success)."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    tid, email = _make_trialing_no_card(days_to_end=1)

    def boom(**kwargs):
        if kwargs.get("to") == email:
            raise RuntimeError("resend down")
        return True

    monkeypatch.setattr(
        scheduler, "send_trial_ending_no_card_reminder_email", boom)

    r = scheduler.send_trial_ending_reminders()
    assert tid not in r["reminded"]
    with SessionLocal() as db:
        assert db.get(Tenant, tid).trial_reminder_sent_at is None


def test_urgent_tenant_gets_one_email_per_sweep_not_two(monkeypatch):
    """A no-card tenant <=2 days out matches BOTH the EARLY (<=7d) and URGENT
    (<=2d) windows. It must get exactly ONE reminder per sweep — the EARLY touch
    now, the URGENT touch on a LATER sweep — never both in a single pass.

    Regression guard for the same-pass double-send: before the `not in reminded`
    filter in send_trial_ending_reminders, a 2-day tenant received two identical
    reminder emails in one run (EARLY stamped it, then URGENT immediately re-fired
    because trial_reminder_sent_at had just become non-NULL)."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    tid, email = _make_trialing_no_card(days_to_end=2)

    calls: list[dict] = []

    def fake_send(**kwargs):
        if kwargs.get("to") == email:
            calls.append(kwargs)
        return True

    monkeypatch.setattr(
        scheduler, "send_trial_ending_no_card_reminder_email", fake_send)

    # Sweep 1: EARLY touch fires once; URGENT is deferred despite qualifying.
    scheduler.send_trial_ending_reminders()
    assert len(calls) == 1, "EARLY and URGENT must not both fire in one sweep"
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.trial_reminder_sent_at is not None
        assert t.trial_final_reminder_sent_at is None

    # Sweep 2: now the URGENT touch fires (its own exactly-once stage).
    scheduler.send_trial_ending_reminders()
    assert len(calls) == 2
    with SessionLocal() as db:
        assert db.get(Tenant, tid).trial_final_reminder_sent_at is not None

    # Sweep 3: both stages stamped → no further sends.
    scheduler.send_trial_ending_reminders()
    assert len(calls) == 2
