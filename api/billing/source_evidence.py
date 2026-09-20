"""Additive invoice evidence archive. Candidate sources are never called exact lineage.

This does not delete or replace operational source rows. The immutable calculated
match is authoritative; original artifacts are retained wherever they still exist.
"""
from datetime import date, datetime
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import json
from sqlalchemy import select, or_

VERSION = "invoice-source-evidence-v1"
SUB_FIELDS = "id tenant_id client_id customer_name source_filename source_workbook parsed_map billing_model allocation_pct array_share_pct crosscheck_threshold_pct array_id utility_account_id array_allocations rate_per_kwh discount_pct net_rate_per_kwh net_rate_adder_per_kwh net_rate_adder_until budget_amount_usd pending_credit_usd cadence annual_trueup client_email operator_email send_mode formats include_summary auto_attach_gmp gmp_invoice_pdf tracker_workbook tracker_map".split()
ARRAY_FIELDS = "id tenant_id client_id name region first_connect_date solar_adder_cents bill_offset_months excluded deleted_at".split()
ACCOUNT_FIELDS = "id tenant_id array_id provider account_number customer_number nickname service_address enabled deleted_at".split()
TENANT_FIELDS = "id name company_name contact_email send_from_email default_billing_rate_per_kwh default_discount_pct default_net_rate_per_kwh default_net_rate_adder_per_kwh default_net_rate_adder_until offtaker_payment_policy".split()
VOLATILE = {"pulled_at", "uploaded_at", "derived_at", "fetched_at", "updated_at", "last_seen"}


def _plain(value):
    if isinstance(value, (date, datetime)): return value.isoformat()
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, bytes): return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    raise TypeError(f"Unsupported evidence value: {type(value).__name__}")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_plain, allow_nan=False).encode()


def _fields(row, fields=None):
    if row is None: return {}
    return {key: getattr(row, key, None) for key in (fields or [c.key for c in row.__table__.columns])}


def collect(sub, *, db=None):
    """Snapshot a conservative same-tenant supporting set, before calculation.

    All account bill history is retained because banked rates can depend on
    earlier statements. Source records are archived separately, so unchanged
    historical originals deduplicate across invoice cycles.
    """
    if db is None:
        from ..db import SessionLocal
        with SessionLocal() as session:
            return collect(sub, db=session)
    from ..models import (Array, UtilityAccount, Tenant, Bill, DailyGeneration,
        GmpDailyGeneration, GmpUsageRaw, RateSchedule, BillingReportSubscription,
        OfftakerInvoiceTemplate, OfftakerSubscriptionTemplate, OfftakerInvoice, OfftakerPayment)
    tid = sub.tenant_id
    entries = []
    def add(kind, row, fields=None):
        if row is not None:
            entries.append({"kind": kind, "row_id": getattr(row, "id", None), "data": _fields(row, fields)})
    add("subscription_calculation_config", sub, SUB_FIELDS)
    current = db.get(BillingReportSubscription, sub.id) if sub.id else None
    if current and current.tenant_id != tid: raise ValueError("Evidence subscription ownership mismatch")
    # Billing uses the supplied subscription values, including reviewed edits
    # not yet committed by the caller; archive those exact values above.
    add("operator_calculation_config", db.get(Tenant, tid), TENANT_FIELDS)
    aids = {sub.array_id} if sub.array_id else set()
    for allocation in getattr(sub, "array_allocations", None) or []:
        if isinstance(allocation, dict) and allocation.get("array_id"):
            aids.add(int(allocation["array_id"]))
    account = db.get(UtilityAccount, sub.utility_account_id) if sub.utility_account_id else None
    if account:
        if account.tenant_id != tid: raise ValueError("Evidence account ownership mismatch")
        if account.array_id: aids.add(account.array_id)
    arrays = list(db.scalars(select(Array).where(Array.id.in_(aids), Array.tenant_id == tid).order_by(Array.id))) if aids else []
    if len(arrays) != len(aids): raise ValueError("Evidence array ownership mismatch")
    for row in arrays: add("arrays", row, ARRAY_FIELDS)
    accounts = list(db.scalars(select(UtilityAccount).where(UtilityAccount.tenant_id == tid,
        or_(UtilityAccount.array_id.in_(aids), UtilityAccount.id == getattr(account, "id", -1))).order_by(UtilityAccount.id)))
    account_ids = [a.id for a in accounts]
    for row in accounts: add("utility_accounts", row, ACCOUNT_FIELDS)
    for model, predicate in ((Bill, Bill.account_id.in_(account_ids)),
        (DailyGeneration, DailyGeneration.array_id.in_(aids)),
        (GmpDailyGeneration, GmpDailyGeneration.account_id.in_(account_ids)),
        (GmpUsageRaw, GmpUsageRaw.account_id.in_(account_ids))):
        for row in db.scalars(select(model).where(model.tenant_id == tid, predicate).order_by(model.id)):
            data = _fields(row)
            # New raw storage may replace the inline CSV. Resolve the artifact
            # through its tenant-checked reader so the invoice remains standalone.
            if model is GmpUsageRaw and not data.get("raw_csv") and data.get("artifact_id"):
                from ..source_artifacts import get_artifact
                data["raw_csv"] = get_artifact(db, tid, data["artifact_id"]).decode("utf-8")
            entries.append({"kind": model.__tablename__, "row_id": row.id, "data": data})
    for model in (OfftakerInvoiceTemplate, OfftakerSubscriptionTemplate):
        query = select(model).where(model.tenant_id == tid)
        if model is OfftakerSubscriptionTemplate: query = query.where(model.subscription_id == sub.id)
        for row in db.scalars(query.order_by(model.id)): add(model.__tablename__, row)
    # Public schedule cells contain no credentials or other customers' records.
    for row in db.scalars(select(RateSchedule).order_by(RateSchedule.id)): add("rate_schedule", row)
    if getattr(sub, "annual_trueup", False):
        for row in db.scalars(select(OfftakerInvoice).where(OfftakerInvoice.tenant_id == tid,
                OfftakerInvoice.subscription_id == sub.id).order_by(OfftakerInvoice.id)):
            add("prior_invoice", row, "id period_key period_start period_end amount_cents credit_applied_cents snapshot status sent_at".split())
        for row in db.scalars(select(OfftakerPayment).where(OfftakerPayment.tenant_id == tid,
                OfftakerPayment.subscription_id == sub.id).order_by(OfftakerPayment.id)):
            add("legacy_budget_payment", row, "id period_key amount_cents status paid_at".split())
    return entries


def _fingerprint(entries):
    # Re-capture freshness alone does not change the evidence values.
    stable = [{**entry, "data": {k:v for k,v in entry["data"].items() if k not in VOLATILE}}
              for entry in entries]
    return hashlib.sha256(_json(stable)).hexdigest()


def finish_capture(sub, match, before):
    if match is None or getattr(match, "_frozen_invoice_id", None): return match
    after = collect(sub)
    if _fingerprint(before) != _fingerprint(after):
        raise ValueError("Invoice source evidence changed during calculation; review and retry")
    match._source_capture = before
    return match


def assert_unchanged(sub, match):
    captured = getattr(match, "_source_capture", None)
    if captured is not None and _fingerprint(captured) != _fingerprint(collect(sub)):
        raise ValueError("Invoice source evidence changed before rendering completed; review and retry")


def capture_calculation(sub, calculate, *, period_label=None):
    if period_label and getattr(sub, "id", None):
        from .issuance import load_frozen
        from .backlog import canonical_period
        frozen = load_frozen(sub.tenant_id, sub.id,
            canonical_period(period_label, getattr(sub, "cadence", "monthly")))
        if frozen is not None: return frozen
    before = collect(sub)
    return finish_capture(sub, calculate(), before)


def archive(db, sub, match):
    """Persist sources within the invoice reservation transaction, or fail it."""
    from ..source_artifacts import put_artifact
    captured = getattr(match, "_source_capture", None)
    during_calculation = captured is not None
    if captured is None: captured = collect(sub, db=db)
    artifacts = []
    def put(payload, kind, row_id=None, field=None, mime="application/json"):
        aid = put_artifact(db, sub.tenant_id, payload, mime_type=mime)
        descriptor = {"artifact_id": aid, "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_length": len(payload), "mime_type": mime, "kind": kind,
            "row_id": row_id, "field": field}
        artifacts.append(descriptor)
        return {"artifact_id": aid, "sha256": descriptor["sha256"]}
    def save_values(value, kind, row_id, field=""):
        if isinstance(value, str) and field in {"raw_csv", "raw_text"}:
            return put(value.encode("utf-8"), kind, row_id, field,
                "text/csv" if field == "raw_csv" else "text/plain")
        if isinstance(value, bytes):
            mime = "application/pdf" if value.startswith(b"%PDF") else "application/octet-stream"
            return put(value, kind, row_id, field, mime)
        if isinstance(value, dict): return {k:save_values(v,kind,row_id,f"{field}.{k}".lstrip(".")) for k,v in value.items()}
        if isinstance(value, list): return [save_values(v,kind,row_id,field) for v in value]
        return value
    for entry in captured:
        data = save_values(entry["data"], entry["kind"], entry["row_id"])
        put(_json(data), entry["kind"], entry["row_id"])
    put(_json(match.to_dict()), "normalized_calculation")
    limitations = [
        "Supporting records are a conservative candidate set, not proven per-field source lineage.",
        "Original daily upload/utility payloads and older source revisions may never have been stored; missing originals cannot be reconstructed.",
        "Fleet reference-rate sample membership is not recorded; the exact rate used is preserved in normalized calculation inputs. Other tenants' records are excluded.",
        "Contract documents are preserved only when present in captured workbook/template/source files; typed contractual settings are preserved as configuration.",
    ]
    if not during_calculation:
        limitations.append("Source capture occurred at reservation, not during calculation; source ancestry is unverified.")
    return {"schema_version": 1, "calculation_archive_version": VERSION,
        "calculation_code_sha256": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("delivery.py", "matcher.py", "trueup.py", "../rate_schedule.py")},
        "deployment_commit": os.getenv("RAILWAY_GIT_COMMIT_SHA") or None,
        "lineage_complete": False, "status": "available_sources_preserved",
        "capture_consistency": "before_after_values_equal" if during_calculation else "unverified",
        "retention": "permanent_no_source_pruning_authorized", "limitations": limitations,
        "artifacts": artifacts}


def manifest(invoice):
    return (invoice.snapshot or {}).get("_source_evidence") or {
        "schema_version": 1, "status": "legacy_lineage_incomplete", "lineage_complete": False,
        "artifacts": [], "limitations": ["This frozen invoice predates source archiving. Its original snapshot and rendered evidence remain unchanged; current source rows cannot prove historical lineage."]}
