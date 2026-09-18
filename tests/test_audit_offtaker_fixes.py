"""Off-taker invoicing audit (2026-09-18) — regression pins for the fixes.

Run from the solar-operator worktree root:
    .venv/Scripts/python -m pytest <this file> -p tests.conftest -q
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import BillingReportSubscription
from api.billing.delivery import _period_guard_label

from tests.test_offtaker_upload import _bulk_import, _make_array_with_bill, _make_tenant

_B = "/v1/array-operator/billing"


# ─── exactly-once guard compares PERIODS, not cycle-end dates ────────────────

def test_period_guard_label_same_month_different_cycle_end_dates_match():
    # Host bill ends 06-30, the offtaker's own June bill ends 06-28: SAME month.
    assert _period_guard_label("2026-06-30") == "2026-06"
    assert _period_guard_label("2026-06-28") == "2026-06"
    assert _period_guard_label("2026-06") == "2026-06"
    assert _period_guard_label("2026-07-31") != _period_guard_label("2026-06-30")


def test_period_guard_label_quarterly_cadence_folds_months_into_the_quarter():
    assert _period_guard_label("2026-06-30", "quarterly") == "2026-Q2"
    assert _period_guard_label("2026-04-30", "quarterly") == "2026-Q2"
    assert _period_guard_label("2026-Q2", "quarterly") == "2026-Q2"
    assert _period_guard_label("2026-07-31", "quarterly") == "2026-Q3"


def test_period_guard_label_trueup_and_blank_never_match_a_billing_period():
    assert _period_guard_label("trueup:2026-08-31") is None
    assert _period_guard_label(None) is None
    assert _period_guard_label("") is None
    assert _period_guard_label("Mar 2026 → Apr 2026") == "Mar 2026 → Apr 2026"


# ─── roster import: a $/kWh Rate column is a RATE, never an 18% discount ─────

def test_bulk_import_rate_column_maps_to_net_rate_not_discount(client):
    tid, auth = _make_tenant()
    aid, ua = _make_array_with_bill(tid, "Maple Street Solar", "GMP-111", with_bill=True)
    lines = ["Customer,Array,% Allocation,Rate ($/kWh),Contact e-mail"]
    for i in range(12):
        lines.append(f"Offtaker {i},Maple Street Solar,{4 + i * 0.5},0.18398,"
                     f"offtaker{i}@example.com")
    lines.append("Total,,100,,")          # footer must not become an offtaker
    csv = "\n".join(lines) + "\n"
    r = _bulk_import(client, auth, "roster.csv", csv.encode(), "text/csv")
    assert r.status_code == 200, r.text
    body = r.json()
    cm = body["detection"]["column_map"]
    assert cm["net_rate"]["index"] == 3, cm
    assert cm.get("discount_pct") is None or cm["discount_pct"].get("index") != 3
    assert cm["allocation_pct"]["index"] == 2
    rows = body["rows"]
    assert len(rows) == 12, [x["offtaker_name"] for x in rows]
    assert all(abs(x["net_rate_per_kwh"] - 0.18398) < 1e-9 for x in rows)
    assert all(x["discount_pct"] is None for x in rows)
    assert abs(rows[0]["allocation_pct"] - 0.04) < 1e-9


def test_bulk_import_flags_and_normalizes_emails(client):
    tid, auth = _make_tenant()
    _make_array_with_bill(tid, "Maple Street Solar", "GMP-111", with_bill=True)
    csv = (
        "Customer,Array,% Allocation,Email\n"
        "Upper Case,Maple Street Solar,10,JACK.MITCHELL@Example.COM\n"
        "Not An Email,Maple Street Solar,10,harper at example dot com\n"
        "Double At,Maple Street Solar,10,james@@example.com\n"
    )
    r = _bulk_import(client, auth, "emails.csv", csv.encode(), "text/csv")
    assert r.status_code == 200, r.text
    body = r.json()
    rows = {x["offtaker_name"]: x for x in body["rows"]}
    assert rows["Upper Case"]["email"] == "jack.mitchell@example.com"
    assert "email_invalid" not in rows["Upper Case"]["flags"]
    for bad in ("Not An Email", "Double At"):
        assert rows[bad]["email"] is None
        assert "email_invalid" in rows[bad]["flags"]
        assert rows[bad]["email_raw"]
        assert rows[bad]["confidence"] not in ("exact", "high")   # demoted to review
    assert body["summary"]["ready"] <= 1
    assert body["summary"]["needs_review"] >= 2


def test_bulk_commit_persists_rate_dedups_batch_and_rejects_bad_email(client):
    tid, auth = _make_tenant()
    aid, ua = _make_array_with_bill(tid, "Maple Street Solar", "GMP-" + secrets.token_hex(2),
                                    with_bill=True)
    row = {"offtaker_name": "Liam Brown", "array_id": aid, "utility_account_id": ua,
           "allocation_pct": 0.045, "email": "Liam@Example.COM",
           "net_rate_per_kwh": 0.18398}
    payload = {
        "rows": [
            row,
            dict(row),                                             # exact duplicate
            {"offtaker_name": "Bad Email", "array_id": aid, "utility_account_id": ua,
             "allocation_pct": 0.05, "email": "harper at example dot com"},
        ],
        "cadence": "monthly", "delivery_mode": "approval",
    }
    r = client.post(f"{_B}/subscriptions/bulk-commit", json=payload,
                    headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1, body
    assert len(body["skipped"]) == 1 and "identical" in body["skipped"][0]["reason"]
    assert len(body["failed"]) == 1 and "not an email" in body["failed"][0]["error"]
    with SessionLocal() as db:
        subs = db.execute(select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tid)).scalars().all()
        assert len(subs) == 1
        s = subs[0]
        assert abs(s.net_rate_per_kwh - 0.18398) < 1e-9     # rate carried, not dropped
        assert s.client_email == "liam@example.com"           # normalized
        assert s.send_mode == "to_client"
        assert s.delivery_mode == "approval"


# ─── approve sends the amount the operator reviewed, or refuses ──────────────

def test_approve_refuses_when_amount_moved_since_review(client):
    from tests.test_billing_delivery import _make_tenant as _mk, _upload
    tid, auth = _mk()
    sid = _upload(client, auth, "norwich.xlsx").json()["subscription"]["id"]
    r = client.post(f"{_B}/subscriptions/{sid}/draft", headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    d = r.json().get("draft") or r.json()
    did = d["id"]
    reviewed = d.get("amount_usd")
    assert reviewed is not None and abs(float(reviewed) - 250.0) > 0.01
    p = client.patch(f"{_B}/subscriptions/{sid}", json={"budget_amount_usd": 250.0},
                     headers={"Authorization": auth})
    assert p.status_code == 200, p.text
    a = client.post(f"{_B}/drafts/{did}/approve", headers={"Authorization": auth})
    assert a.status_code == 422, a.text
    assert "amount changed" in a.json()["detail"].lower()
    with SessionLocal() as db:
        assert db.get(BillingReportSubscription, sid).last_sent_at is None
