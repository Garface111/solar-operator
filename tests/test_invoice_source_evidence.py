from datetime import date
import json
import pytest
from sqlalchemy import select
from api.db import SessionLocal
from api.models import (Bill, BillingReportSubscription, OfftakerInvoice, Tenant,
    SourceArtifact, SourceArtifactChunk, UtilityAccount, Array)
from api.billing import delivery, issuance, source_evidence
from api.source_artifacts import get_artifact
from tests.test_offtaker_utility_bill import _seed


def prepared_sub():
    tid, aid, account = _seed(with_bill=True)
    with SessionLocal() as db:
        sub = BillingReportSubscription(tenant_id=tid, customer_name="Evidence Customer",
            utility_account_id=account, array_id=aid, allocation_pct=.5,
            billing_model="percent_of_array", cadence="monthly", client_email="customer@example.test")
        db.add(sub); db.commit(); db.refresh(sub)
        db.expunge(sub)
    return sub


def freeze_sub(sub):
    match = source_evidence.capture_calculation(sub, lambda: delivery.build_match(sub))
    end = match.computed_invoice["period_end"]
    key = end[:7]
    iid, restored, _ = issuance.freeze(tenant_id=sub.tenant_id, subscription_id=sub.id,
        key=key, match=match, prepare=lambda m: {"attachments": [{"filename":"original.pdf", "content":"b3JpZ2luYWw="}], "variants":{}})
    return iid, restored


def test_original_bill_and_config_preserved_after_source_correction():
    sub = prepared_sub()
    original_pdf = b"%PDF source statement original bytes\r\n"
    with SessionLocal() as db:
        bill = db.scalar(select(Bill).where(Bill.account_id == sub.utility_account_id))
        bill.pdf_bytes = original_pdf
        bill.raw_json = {"utility_original": {"excess":123, "unmodeled":"preserve me"}}
        account = db.get(UtilityAccount, sub.utility_account_id)
        account.extra = {"access_token":"must-not-copy-account-secret"}
        db.get(Array, sub.array_id).solaredge_api_key = "must-not-copy-array-secret"
        db.commit()
    iid, result = freeze_sub(sub)
    with SessionLocal() as db:
        invoice = db.get(OfftakerInvoice, iid)
        manifest = source_evidence.manifest(invoice)
        assert manifest["capture_consistency"] == "before_after_values_equal"
        assert manifest["lineage_complete"] is False
        records = [a for a in manifest["artifacts"] if a["kind"] == "bills" and not a["field"]]
        archived_bill = json.loads(get_artifact(db,sub.tenant_id,records[0]["artifact_id"]))
        assert archived_bill["raw_json"]["utility_original"]["unmodeled"] == "preserve me"
        assert get_artifact(db, sub.tenant_id, archived_bill["pdf_bytes"]["artifact_id"]) == original_pdf
        all_bytes = b" ".join(get_artifact(db, sub.tenant_id, a["artifact_id"]) for a in manifest["artifacts"])
        assert b"must-not-copy" not in all_bytes
        bill = db.get(Bill, archived_bill["id"])
        bill.pdf_bytes = b"corrected PDF"; bill.raw_json = {"new":"data"}; db.commit()
        assert get_artifact(db, sub.tenant_id, archived_bill["pdf_bytes"]["artifact_id"]) == original_pdf
        assert issuance.restore(invoice.snapshot).computed_invoice == result.computed_invoice


def test_capture_holds_when_source_changes_during_calculation():
    sub = prepared_sub()
    def calculate():
        match = delivery.build_match(sub)
        with SessionLocal() as db:
            bill = db.scalar(select(Bill).where(Bill.account_id == sub.utility_account_id))
            bill.solar_credit_usd = float(bill.solar_credit_usd or 0) + 10
            db.commit()
        return match
    with pytest.raises(ValueError, match="changed during calculation"):
        source_evidence.capture_calculation(sub, calculate)


def test_archival_failure_rolls_back_invoice_credit_and_number(monkeypatch):
    sub = prepared_sub()
    with SessionLocal() as db:
        row = db.get(BillingReportSubscription, sub.id)
        row.pending_credit_usd = 10; row.invoice_number_next = 42; db.commit()
    def unavailable(*args, **kwargs): raise RuntimeError("archive unavailable")
    monkeypatch.setattr("api.source_artifacts.put_artifact", unavailable)
    with pytest.raises(RuntimeError, match="archive unavailable"): freeze_sub(sub)
    with SessionLocal() as db:
        row = db.get(BillingReportSubscription, sub.id)
        assert row.pending_credit_usd == 10 and row.invoice_number_next == 42
        assert db.scalar(select(OfftakerInvoice).where(OfftakerInvoice.subscription_id == sub.id)) is None


def test_frozen_retry_never_rereads_changed_sources(monkeypatch):
    sub = prepared_sub()
    iid, match = freeze_sub(sub)
    def forbidden(*args, **kwargs): raise AssertionError("must not reread live sources")
    monkeypatch.setattr(source_evidence, "collect", forbidden)
    restored = source_evidence.capture_calculation(sub, forbidden,
        period_label=match.computed_invoice["period_end"][:7])
    assert restored.computed_invoice == match.computed_invoice
    assert restored._frozen_invoice_id == iid


def test_evidence_download_requires_owner_and_invoice_link(client):
    from api.account import mint_session_for_tenant
    from tests.test_offtaker_upload import _make_tenant
    sub = prepared_sub()
    iid, _ = freeze_sub(sub)
    path = f"/v1/array-operator/billing/invoices/{iid}/source-evidence"
    auth = {"Authorization": "Bearer " + mint_session_for_tenant(sub.tenant_id)}
    assert client.get(path).status_code == 401
    result = client.get(path, headers=auth)
    assert result.status_code == 200
    artifact = result.json()["artifacts"][0]
    download = client.get(path + f"/{artifact['artifact_id']}", headers=auth)
    assert download.status_code == 200
    assert download.headers["cache-control"] == "private, no-store"
    _, other_auth = _make_tenant()
    assert client.get(path, headers={"Authorization":other_auth}).status_code == 404
    assert client.get(path + f"/{artifact['artifact_id']}",headers={"Authorization":other_auth}).status_code == 404
    assert client.get(path + "/987654321",headers=auth).status_code == 404


def test_legacy_manifest_never_claims_reconstructed_history():
    legacy = OfftakerInvoice(snapshot={"computed_invoice":{"amount_owed":123}})
    original = dict(legacy.snapshot)
    assert source_evidence.manifest(legacy)["status"] == "legacy_lineage_incomplete"
    assert legacy.snapshot == original


def test_raw_artifact_source_preserved_and_identical_files_deduplicate():
    from api.models import GmpUsageRaw
    from api.source_artifacts import put_artifact
    sub = prepared_sub()
    raw = b"Date,Generated\r\n2026-05-01,42\r\n"
    with SessionLocal() as db:
        aid = put_artifact(db, sub.tenant_id, raw, mime_type="text/csv")
        db.add(GmpUsageRaw(tenant_id=sub.tenant_id, account_id=sub.utility_account_id,
            account_number="meter",window_start=date(2026,5,1),window_end=date(2026,5,31),
            raw_csv=None,artifact_id=aid))
        db.commit()
    match = source_evidence.capture_calculation(sub, lambda: delivery.build_match(sub))
    with SessionLocal() as db:
        first = source_evidence.archive(db, sub, match)
        second = source_evidence.archive(db, sub, match)
        assert [a["artifact_id"] for a in first["artifacts"]] == [a["artifact_id"] for a in second["artifacts"]]
        record = next(a for a in first["artifacts"] if a["kind"] == "gmp_usage_raw" and not a["field"])
        content = json.loads(get_artifact(db, sub.tenant_id, record["artifact_id"]))
        assert content["raw_csv"]["artifact_id"] == aid
        assert get_artifact(db, sub.tenant_id, content["raw_csv"]["artifact_id"]) == raw
        db.rollback()


def test_renderer_cannot_silently_use_sources_changed_after_calculation(monkeypatch):
    sub = prepared_sub()
    sub.operator_email = "operator@example.test"
    match = source_evidence.capture_calculation(sub, lambda: delivery.build_match(sub))
    with SessionLocal() as db:
        bill = db.scalar(select(Bill).where(Bill.account_id == sub.utility_account_id))
        bill.pdf_bytes = b"source changed between math and rendering"
        db.commit()
        tenant = db.get(Tenant, sub.tenant_id)
        def forbidden(*a, **kw): raise AssertionError("must detect source drift before render")
        monkeypatch.setattr(delivery, "generate_files", forbidden)
        with pytest.raises(ValueError, match="changed before rendering"):
            delivery._prepare_invoice_evidence(match, sub, tenant)


def test_all_candidate_period_pdfs_workbook_and_daily_values_are_preserved():
    from datetime import datetime
    from api.models import DailyGeneration, OfftakerSubscriptionTemplate
    sub = prepared_sub()
    sub.source_workbook = b"original uploaded workbook bytes"
    with SessionLocal() as db:
        for month in (4,6):
            db.add(Bill(tenant_id=sub.tenant_id, account_id=sub.utility_account_id,
                period_start=datetime(2026,month,1),period_end=datetime(2026,month,30),
                pdf_bytes=f"%PDF month {month}".encode(),kwh_generated=500,
                kwh_sent_to_grid=400,solar_credit_usd=100))
        db.add(DailyGeneration(tenant_id=sub.tenant_id,array_id=sub.array_id,
            day=date(2026,7,1),kwh=42.5,source="utility_meter"))
        db.add(OfftakerSubscriptionTemplate(tenant_id=sub.tenant_id,subscription_id=sub.id,
            file_bytes=b"template original",html="<p>original template</p>",enabled=False))
        db.commit()
    iid, _ = freeze_sub(sub)
    with SessionLocal() as db:
        artifacts = source_evidence.manifest(db.get(OfftakerInvoice,iid))["artifacts"]
        payloads = [get_artifact(db,sub.tenant_id,a["artifact_id"]) for a in artifacts]
        assert b"original uploaded workbook bytes" in payloads
        assert b"template original" in payloads
        assert b"%PDF month 4" in payloads and b"%PDF month 6" in payloads
        daily = [json.loads(get_artifact(db,sub.tenant_id,a["artifact_id"]))
                 for a in artifacts if a["kind"] == "daily_generation"]
        assert any(d["kwh"] == 42.5 and d["source"] == "utility_meter" for d in daily)
