"""Automatic bill-adapter discovery — safety bounds + GMP/VEC offline proofs.

Live portal logins are NOT exercised here (no customer passwords). We prove:
  * MFA/CAPTCHA/lockout page detection aborts safely
  * Offline synthesis from captured JSON (the post-HAR half of the pipeline)
  * GMP + VEC-shaped captures synthesize / map correctly
  * Known-family enqueue short-circuits without browser (skipped_known)
  * Unknown enqueue creates a queued job
"""
from __future__ import annotations

import json
import secrets
import pytest

from sqlalchemy import select

from api.db import SessionLocal, init_db
from api.models import Tenant, BillDiscoveryJob
from api.bill_discovery_engine import (
    page_requires_abort,
    url_looks_billish,
    run_discovery_from_captures,
    enqueue_discovery,
    _synthesize_from_captures,
)


@pytest.fixture(autouse=True)
def isolated_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr("api.auto_adapters._DB", str(tmp_path / "adapters.db"))
    monkeypatch.setattr("api.auto_adapters.agent", lambda *a, **k: (None, "disabled in test"))
    monkeypatch.setattr("api.bill_discovery_engine._spawn_process_one", lambda *a: None)


def test_abort_detects_captcha_and_mfa():
    assert page_requires_abort("Please complete the reCAPTCHA") == "captcha"
    assert page_requires_abort("Enter your two-factor authentication code") == "mfa"
    assert page_requires_abort("Your account has been locked") == "account_locked"
    assert page_requires_abort("Welcome to your billing dashboard") is None


def test_url_billish():
    assert url_looks_billish("https://x/services/secured/billing/history/overview")
    assert url_looks_billish("https://api.example.com/v2/accounts/1/bills")
    assert not url_looks_billish("https://cdn.example.com/logo.png")


def test_offline_synthesis_from_generation_json():
    """Simulates network captures the browser would have collected."""
    body = json.dumps({
        "records": [
            {"billDate": "2026-05-01", "solarGenerationKwh": 400.0},
            {"billDate": "2026-06-01", "solarGenerationKwh": 450.0},
        ],
        "totalGenerationKwh": 850.0,
    })
    captures = [{
        "url": "https://portal.example/api/billing/history",
        "status": 200,
        "content_type": "application/json",
        "bytes": len(body),
        "body": body,
    }]
    result = run_discovery_from_captures(captures, provider="acme_power")
    # Heuristic may or may not match this shape — never crash; status set.
    assert result["status"] in ("candidate", "failed")
    assert "captures" in result


def test_gmp_shaped_capture_synthesizes_or_parses():
    """GMP bill list as if captured from network — metrics path still works
    even when auto_adapters heuristic doesn't match nested segmentLineItems."""
    from api.adapters import gmp

    bill = {
        "billNumber": "D1",
        "billDate": "2026-06-15",
        "billSegments": [{
            "startDate": "2026-05-15",
            "endDate": "2026-06-14",
            "segmentLineItems": [
                {"unitOfMeasure": "KWH", "unitCode": "GENERATE", "unitCount": 900.0},
                {"unitOfMeasure": "KWH", "unitCode": "EXCESS", "unitCount": 850.0},
            ],
            "segmentCalcs": [
                {"startDate": "2026-05-15", "endDate": "2026-06-14", "dollarAmount": -40.0},
            ],
        }],
    }
    # Family adapter proof (production path for GMP).
    m = gmp.bill_json_to_metrics(bill)
    assert m["kwh_generated"] == 900
    assert m["kwh_sent_to_grid"] == 850.0

    # Discovery offline path with a list payload (bills array).
    body = json.dumps([bill])
    result = _synthesize_from_captures([{
        "url": "https://api.greenmountainpower.com/api/v2/accounts/1/bills",
        "status": 200,
        "content_type": "application/json",
        "body": body,
        "bytes": len(body),
    }], provider="gmp")
    assert result["status"] in ("candidate", "failed")
    assert len(result["captures"]) == 1


def test_vec_shaped_capture_maps_bills():
    from api.harvester.vendors.smarthub import SmartHubVendor
    from pathlib import Path

    fixture = Path("tests/fixtures/vec/billing_rows.json")
    rows = json.loads(fixture.read_text())
    # Treat as already-shaped captures + raw overview
    raw = {
        "acctNbr": "6578300",
        "custName": "TEST",
        "billingDateTimestamp": 1700006400000,
        "adjustedBillAmount": -100.0,
        "billProcessUuid": "u1",
        "systemOfRecord": "UTILITY",
        "servLocs": [{}],
    }
    mapped = SmartHubVendor._bill_row("6578300", raw)
    assert mapped["billing_date"]
    assert mapped["account_id"] == "6578300"
    assert len(rows) >= 1


def test_enqueue_known_family_skips_browser():
    init_db()
    tid = "ten_disc_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, tenant_key="sol_" + secrets.token_hex(8),
            name="Disc", contact_email=f"{tid}@t.test",
            active=True, product="array_operator",
        ))
        db.commit()

    job = enqueue_discovery(
        tenant_id=tid, provider="gmp", username="owner@x.com",
        force_explore=False,
    )
    assert job["status"] == "skipped_known"
    assert job["family"] == "gmp"
    assert job["action"] == "arm_known"

    job2 = enqueue_discovery(
        tenant_id=tid, provider="vec", username="owner@x.com",
        login_host="vermontelectric.smarthub.coop",
    )
    assert job2["status"] == "skipped_known"
    assert job2["family"] == "smarthub"


def test_discovery_candidate_never_activates_or_manufactures_bills(monkeypatch):
    """Successful generation extraction is not evidence of a utility bill."""
    from api import bill_discovery_engine as discovery
    from api import auto_adapters as aa
    from api.bill_adapter_autopilot import synthesize_bill_extractor
    from api.models import Bill, UtilityAccount

    init_db()
    tid = "ten_quarantine_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key="sol_" + secrets.token_hex(8),
                      name="Quarantine", contact_email=f"{tid}@t.test",
                      active=True, product="array_operator"))
        db.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery must not approve, ingest or schedule capture")

    monkeypatch.setattr(aa, "reg_approve", forbidden)
    monkeypatch.setattr(discovery, "_trigger_harvest_async", forbidden)
    monkeypatch.setattr("api.worker._upsert_bill", forbidden)
    monkeypatch.setattr("api.worker.pull_bills_for_tenant", forbidden)
    notices = []
    monkeypatch.setattr("api.bill_adapter_autopilot.notify_new_bill_adapter",
                        lambda **kw: notices.append(kw))
    body = json.dumps({"items": [
        {"date": "2026-05-01", "generation_kwh": 111.0},
        {"date": "2026-06-01", "generation_kwh": 222.0},
    ]})  # Deliberately no independent total: structural synthesis only.
    captures = [{"url": "https://portal.acme/api/billing/history", "status": 200,
                 "content_type": "application/json", "body": body, "bytes": len(body)}]
    syn = synthesize_bill_extractor(body, provider="acme_power", notify=False)
    assert syn["ok"] and syn["spec"]
    assert syn["reconcile"] is None
    assert aa.extract(syn["spec"], body)[0]  # Real successful extraction.

    # Direct/API compatibility callers cannot bypass using a known-family label.
    for provider in ("acme_power", "gmp", "vec", "sh_new_utility"):
        result = discovery.activate_adapter_and_start_capture(
            tenant_id=tid, provider=provider, username_lc="owner@x.com",
            synthesis=syn, captures=captures)
        assert result["status"] == "candidate"
        assert result["ok"] is False and result["activated"] is False
        assert result["ready"] is False and result["approved"] == 0
        assert result["bills_created"] == result["bills_updated"] == 0
        assert result["credentials_rearmed"] == 0

    offline = discovery.run_discovery_from_captures(
        captures, provider="acme_power", tenant_id=tid,
        username_lc="owner@x.com", start_capture=True)
    assert offline["status"] == "candidate" and offline["ready"] is False
    assert offline["capture_start"]["activated"] is False
    assert offline["synthesis"]["spec"]

    job = discovery.enqueue_discovery(tenant_id=tid, provider="acme_power",
                                     username="owner@x.com", force_explore=True)
    finalized = discovery._finalize_job(job["id"], {
        "status": "succeeded", "synthesis": syn, "captures": captures,
        "detail": "Extraction succeeded",
    })
    assert finalized["status"] == "candidate"
    assert finalized["ready"] is False and finalized["activated"] is False
    assert finalized["synthesis"]["spec"] == syn["spec"]
    assert "not activated" in finalized["detail"]
    assert notices and "requires review" in notices[-1]["detail"]
    assert aa.reg_get(syn["fingerprint"])["status"] == "candidate"
    with SessionLocal() as db:
        assert not db.execute(select(Bill).where(Bill.tenant_id == tid)).scalars().all()
        assert not db.execute(select(UtilityAccount).where(UtilityAccount.tenant_id == tid)).scalars().all()
        stored = db.get(BillDiscoveryJob, job["id"])
        assert json.loads(stored.captures_json)[0]["body_sample"] == body


def test_notify_new_bill_adapter_sends_internal_alert(monkeypatch):
    """Ford gets an email when a candidate adapter is stored."""
    sent = {}

    def _fake_alert(subject, body, to=None):
        sent["subject"] = subject
        sent["body"] = body
        sent["to"] = to
        return True

    monkeypatch.setattr("api.notify.send_internal_alert", _fake_alert)
    from api.bill_adapter_autopilot import notify_new_bill_adapter
    ok = notify_new_bill_adapter(
        provider="acme_power",
        fingerprint="fp_test_123",
        source="heuristic",
        tenant_id="ten_x",
        username="owner@x.com",
        job_id=42,
        detail="unit test",
    )
    assert ok is True
    assert "acme_power" in sent["subject"]
    assert "fp_test_123" in sent["body"]
    assert "ten_x" in sent["body"]
    assert "42" in sent["body"]


def test_enqueue_unknown_queues_explore():
    init_db()
    tid = "ten_unk_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, tenant_key="sol_" + secrets.token_hex(8),
            name="Unk", contact_email=f"{tid}@t.test",
            active=True, product="array_operator",
        ))
        db.commit()

    job = enqueue_discovery(
        tenant_id=tid, provider="acme_power", username="owner@x.com",
        login_host="portal.acme.example",
    )
    # Thread may already have started processing — queued or failed (no creds).
    assert job["status"] in ("queued", "running", "failed", "aborted_safe")
    assert job["action"] == "explore"
    assert job["id"]


def test_stored_discovery_samples_redact_secrets_but_preserve_billing():
    from api.bill_discovery_engine import _finalize_job
    init_db()
    tid = "ten_redact_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key="sol_" + secrets.token_hex(8), name="Redact",
                      contact_email=f"{tid}@t.test", active=True, product="array_operator"))
        db.commit()
    job = enqueue_discovery(tenant_id=tid, provider="acme_power", username="owner@x.com")
    secrets_by_key = {key: "SENSITIVE_" + key for key in (
        "password", "access_token", "refreshToken", "id_token", "Authorization",
        "Cookie", "Set-Cookie", "sessionId", "session_secret", "apiKey", "jwt")}
    body = json.dumps({"account_number": "123456", "amount": 206.98,
                       "nested": [secrets_by_key],
                       "headers": [{"name": "Authorization", "value": "SENSITIVE_header"}]})
    unsafe_url = "https://SENSITIVE_user:SENSITIVE_pass@portal.example/bills?token=SENSITIVE_query#SENSITIVE_fragment"
    _finalize_job(job["id"], {"status": "failed", "captures": [
        {"url": unsafe_url, "body": body, "content_type": "application/json"},
        {"url": unsafe_url, "body": "<html>SENSITIVE_html</html>", "content_type": "text/html"},
        {"url": unsafe_url, "body": '{"password":"SENSITIVE_broken"', "content_type": "application/json"},
    ], "synthesis": {"ok": False, "_source_url": unsafe_url}})
    with SessionLocal() as db:
        stored = db.get(BillDiscoveryJob, job["id"])
        assert "SENSITIVE_" not in stored.captures_json
        assert "SENSITIVE_" not in stored.synthesis_json
        captures = json.loads(stored.captures_json)
        assert all(c["url"] == "https://portal.example/bills" for c in captures)
        sample = json.loads(captures[0]["body_sample"])
        assert sample["account_number"] == "123456"
        assert sample["amount"] == 206.98
        assert all(v == "[REDACTED]" for v in sample["nested"][0].values())
        assert sample["headers"][0]["value"] == "[REDACTED]"
        assert captures[1]["body_sample"] == "[non-JSON sample omitted]"
        assert captures[2]["body_preview"] == "[non-JSON sample omitted]"
