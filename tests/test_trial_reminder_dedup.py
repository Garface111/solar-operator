"""
Trial-end reminder is exactly-once via trial_reminder_sent_at (SWEEP Task 2).

The old logic deduped on a 1-day rolling window (now+2d < trial_ends_at <=
now+3d), which dropped or duplicated the email if the daily tick was missed or
fired twice. The new logic selects trialing no-card tenants within 3 days whose
trial_reminder_sent_at IS NULL, then stamps it after a successful send — so the
reminder fires exactly once regardless of tick cadence.
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
    tid, email = _make_trialing_no_card(days_to_end=2)

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
