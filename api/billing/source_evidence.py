"""Additive invoice evidence archive. Candidate sources are never called exact lineage.

This does not delete or replace operational source rows. The immutable calculated
match is authoritative; original artifacts are retained wherever they still exist.
"""
from datetime import date, datetime
import calendar
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import json
from sqlalchemy import select, or_, func, extract

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


# Hard bounds are independent of account age or recapture frequency. Limits are
# reported, never represented as proof of exhaustive source lineage.
MAX_BILLS = 32
MAX_RAW_WINDOWS = 96
MAX_RAW_PER_ACCOUNT = 3
MAX_DAILY_ROWS = 12000
MAX_ACCOUNTS = 128
BILL_META = "id tenant_id account_id document_number bill_date period_start period_end kwh_generated kwh_consumed kwh_sent_to_grid solar_credit_usd parse_status pulled_at".split()
RAW_META = "id tenant_id account_id account_number window_start window_end interval_min interval_max http_status row_count fetched_at artifact_id".split()


def _period(match=None, start=None, end=None):
    ci = match.computed_invoice if match is not None else {}
    start, end = start or ci.get("period_start"), end or ci.get("period_end")
    return (date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])) if start and end else (None, None)


def collect(sub, *, match=None, db=None, period_start=None, period_end=None):
    """Build a bounded metadata plan. Never fetch historical raw CSV/PDF bodies.

    Bill blobs are deferred until archive(), one selected row at a time. Existing
    raw artifacts are linked by identity without decompression. Historical rate
    sample ancestry stays explicitly unverified; this is not permission to prune.
    """
    if db is None:
        from ..db import SessionLocal
        with SessionLocal() as session:
            return collect(sub, match=match, db=session, period_start=period_start, period_end=period_end)
    from ..models import (Array, UtilityAccount, Tenant, Bill, DailyGeneration,
        GmpDailyGeneration, GmpUsageRaw, RateSchedule, OfftakerInvoiceTemplate,
        OfftakerSubscriptionTemplate, OfftakerInvoice, OfftakerPayment)
    tid = sub.tenant_id
    start, end = _period(match, period_start, period_end)
    ci = match.computed_invoice if match is not None else {}
    source = str(ci.get("kwh_source") or "")
    is_trueup = bool(ci.get("is_trueup")) or (match is None and bool(start and end))
    # A workbook-only invoice does not acquire unrelated generation history.
    manual = not sub.source_workbook or (sub.utility_account_id is not None and sub.allocation_pct is not None)
    need_daily = manual and (is_trueup or "daily" in source or "smarthub" in source or "bill_prorate" in source)
    need_gmp = manual and (is_trueup or "gmp_api" in source)
    entries = []
    def add(kind, data, deferred=False):
        entries.append({"kind":kind, "row_id":data.get("id"), "data":dict(data), "deferred":deferred})
    add("capture_scope", {"period_start":start, "period_end":end,
        "bounds":{"bills":MAX_BILLS,"raw_windows":MAX_RAW_WINDOWS,"raw_per_account":MAX_RAW_PER_ACCOUNT,
                  "daily_rows":MAX_DAILY_ROWS,"accounts":MAX_ACCOUNTS},
        "historical_rate_sample_lineage":"not_copied"})
    # These are the exact caller-held values used by calculation, including
    # reviewed edits not yet committed. No extra subscription blob SELECT.
    add("subscription_calculation_config", _fields(sub, SUB_FIELDS))
    tenant_cols = [getattr(Tenant,k) for k in TENANT_FIELDS if hasattr(Tenant,k)]
    tenant = db.execute(select(*tenant_cols).where(Tenant.id == tid)).mappings().first()
    if tenant: add("operator_calculation_config", tenant)
    aids = {sub.array_id} if sub.array_id else set()
    for allocation in getattr(sub,"array_allocations",None) or []:
        if isinstance(allocation,dict) and allocation.get("array_id"): aids.add(int(allocation["array_id"]))
    acols = [getattr(UtilityAccount,k) for k in ACCOUNT_FIELDS if hasattr(UtilityAccount,k)]
    own = db.execute(select(*acols).where(UtilityAccount.id == sub.utility_account_id,
        UtilityAccount.tenant_id == tid)).mappings().first() if sub.utility_account_id else None
    if sub.utility_account_id and not own: raise ValueError("Evidence account ownership mismatch")
    if own and own["array_id"]: aids.add(own["array_id"])
    array_cols = [getattr(Array,k) for k in ARRAY_FIELDS if hasattr(Array,k)]
    arrays = db.execute(select(*array_cols).where(Array.id.in_(aids),Array.tenant_id == tid).order_by(Array.id)).mappings().all() if aids else []
    if len(arrays) != len(aids): raise ValueError("Evidence array ownership mismatch")
    for row in arrays: add("arrays",row)
    accounts = {own["id"]:own} if own else {}
    # Bill calculations may consult the group's first/host account, not every
    # neighboring offtaker. Generation aggregation can consume several meters.
    for aid in sorted(aids):
        q = select(*acols).where(UtilityAccount.tenant_id == tid,
            UtilityAccount.array_id == aid, UtilityAccount.deleted_at.is_(None)).order_by(UtilityAccount.id)
        for row in db.execute(q.limit(MAX_ACCOUNTS if need_gmp else 1)).mappings():
            if len(accounts) < MAX_ACCOUNTS or row["id"] in accounts: accounts[row["id"]] = row
    for aid in sorted(accounts): add("utility_accounts",accounts[aid])
    account_ids = list(accounts)
    if start and end:
        # At most two revisions per account/end-month, then a global bound.
        # Projection deliberately excludes raw_json/raw_text/pdf_bytes.
        bill_cols = [getattr(Bill,k) for k in BILL_META]
        bill_start = date(start.year,start.month,1) if is_trueup or ci.get("billing_cadence") == "quarterly" else date(end.year,end.month,1)
        bill_end = date(end.year,end.month,calendar.monthrange(end.year,end.month)[1])
        ranked = select(*bill_cols, func.length(Bill.pdf_bytes).label("pdf_byte_length"),
            func.row_number().over(partition_by=(Bill.account_id,extract("year",Bill.period_end),extract("month",Bill.period_end)),
                order_by=(Bill.period_end.desc(),Bill.id.desc())).label("_rank")).where(
                Bill.tenant_id == tid, Bill.account_id.in_(account_ids),
                Bill.period_end >= datetime.combine(bill_start,datetime.min.time()),
                Bill.period_end <= datetime.combine(bill_end,datetime.max.time()),
                Bill.period_start <= datetime.combine(end,datetime.max.time())).subquery()
        for row in db.execute(select(ranked).where(ranked.c._rank <= 2)
                .order_by(ranked.c.period_end.desc(),ranked.c.id.desc()).limit(MAX_BILLS)).mappings():
            data=dict(row);data.pop("_rank");add("bills",data,True)
        if need_daily:
            for row in db.execute(select(DailyGeneration).where(DailyGeneration.tenant_id == tid,
                    DailyGeneration.array_id.in_(aids),DailyGeneration.day.between(start,end))
                    .order_by(DailyGeneration.id).limit(MAX_DAILY_ROWS)).scalars():
                add("daily_generation",_fields(row))
        if need_gmp:
            used_accounts=set()
            for row in db.execute(select(GmpDailyGeneration).where(GmpDailyGeneration.tenant_id == tid,
                    GmpDailyGeneration.account_id.in_(account_ids),GmpDailyGeneration.day.between(start,end))
                    .order_by(GmpDailyGeneration.id).limit(MAX_DAILY_ROWS)).scalars():
                add("gmp_daily_generation",_fields(row));used_accounts.add(row.account_id)
            # Select metadata only, never raw_csv. Frequency of historical pulls
            # cannot increase this bounded supporting set or its memory use.
            ranked = select(*[getattr(GmpUsageRaw,k) for k in RAW_META],
                func.row_number().over(partition_by=GmpUsageRaw.account_id,
                    order_by=(GmpUsageRaw.fetched_at.desc(),GmpUsageRaw.id.desc())).label("_rank")).where(
                    GmpUsageRaw.tenant_id == tid,GmpUsageRaw.account_id.in_(used_accounts),
                    GmpUsageRaw.window_start <= end,GmpUsageRaw.window_end >= start,
                    GmpUsageRaw.http_status == 200).subquery()
            for row in db.execute(select(ranked).where(ranked.c._rank <= MAX_RAW_PER_ACCOUNT)
                    .order_by(ranked.c.account_id,ranked.c._rank).limit(MAX_RAW_WINDOWS)).mappings():
                data=dict(row);data.pop("_rank");add("gmp_usage_raw",data)
        rates = select(RateSchedule).where(RateSchedule.effective_start <= end,
            or_(RateSchedule.effective_end.is_(None),RateSchedule.effective_end >= start),
            RateSchedule.utility.in_({a["provider"] for a in accounts.values()} | {"*"}))
        for row in db.scalars(rates.order_by(RateSchedule.id).limit(64)): add("rate_schedule",_fields(row))
    for model in (OfftakerInvoiceTemplate,OfftakerSubscriptionTemplate):
        cols=[model.id,model.tenant_id,model.filename,model.content_type,model.enabled,model.updated_at,
              func.length(model.file_bytes).label("file_byte_length"),func.length(model.html).label("html_length")]
        q=select(*cols).where(model.tenant_id == tid)
        if model is OfftakerSubscriptionTemplate: q=q.where(model.subscription_id == sub.id)
        for row in db.execute(q.limit(1)).mappings(): add(model.__tablename__,row,True)
    if is_trueup and start and end:
        # Preserve invoice identity and normalized obligations without embedding
        # old render payloads/manifests recursively into each new invoice.
        fields="id period_key period_start period_end amount_cents credit_applied_cents status sent_at".split()
        q=select(*[getattr(OfftakerInvoice,k) for k in fields]).where(OfftakerInvoice.tenant_id == tid,
            OfftakerInvoice.subscription_id == sub.id,OfftakerInvoice.period_start <= end,
            OfftakerInvoice.period_end >= start).order_by(OfftakerInvoice.id).limit(24)
        for row in db.execute(q).mappings(): add("prior_invoice",row)
        fields="id period_key amount_cents status paid_at".split()
        q=select(*[getattr(OfftakerPayment,k) for k in fields]).where(OfftakerPayment.tenant_id == tid,
            OfftakerPayment.subscription_id == sub.id,OfftakerPayment.period_key >= start.isoformat()[:7],
            OfftakerPayment.period_key <= end.isoformat()).order_by(OfftakerPayment.id).limit(24)
        for row in db.execute(q).mappings(): add("legacy_budget_payment",row)
    return entries


def _fingerprint(entries):
    def stable(entry):
        data=entry["data"]
        raw = "window_start" in data and "window_end" in data and "account_id" in data
        ignored = (VOLATILE - {"fetched_at"}) | {"artifact_id"} if raw else VOLATILE
        # Inline -> artifact compaction changes representation, not source input.
        # For raw windows, capture time remains meaningful: a new utility pull
        # must not masquerade as a harmless storage conversion.
        return {**entry,"data":{k:v for k,v in data.items() if k not in ignored}}
    return hashlib.sha256(_json([stable(entry) for entry in entries])).hexdigest()


def finish_capture(sub, match, before):
    if match is None or getattr(match, "_frozen_invoice_id", None): return match
    after = collect(sub, match=match)
    if _fingerprint(before) != _fingerprint(after):
        raise ValueError("Invoice source evidence changed during calculation; review and retry")
    match._source_capture = before
    return match


def assert_unchanged(sub, match):
    captured = getattr(match, "_source_capture", None)
    if captured is not None and _fingerprint(captured) != _fingerprint(collect(sub, match=match)):
        raise ValueError("Invoice source evidence changed before rendering completed; review and retry")


def capture_calculation(sub, calculate, *, period_label=None):
    if period_label and getattr(sub, "id", None):
        from .issuance import load_frozen
        from .backlog import canonical_period
        frozen = load_frozen(sub.tenant_id, sub.id,
            canonical_period(period_label, getattr(sub, "cadence", "monthly")))
        if frozen is not None: return frozen
    match = calculate()
    if match is None or getattr(match, "_frozen_invoice_id", None): return match
    before = collect(sub, match=match)
    # Resolve the actual period before collecting sources. Re-evaluation checks
    # the authoritative result while metadata checks detect in-scope changes.
    checked = calculate()
    if _json(match.to_dict()) != _json(checked.to_dict()):
        raise ValueError("Invoice source evidence changed during calculation; review and retry")
    return finish_capture(sub, match, before)


def archive(db, sub, match):
    """Persist sources within the invoice reservation transaction, or fail it."""
    from ..source_artifacts import put_artifact
    captured = getattr(match, "_source_capture", None)
    during_calculation = captured is not None
    if captured is None: captured = collect(sub, match=match, db=db)
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
    from ..models import Bill, OfftakerInvoiceTemplate, OfftakerSubscriptionTemplate, SourceArtifact, GmpUsageRaw
    deferred_models = {m.__tablename__:m for m in (Bill,OfftakerInvoiceTemplate,OfftakerSubscriptionTemplate)}
    for entry in captured:
        values = entry["data"]
        if entry.get("deferred"):
            model = deferred_models[entry["kind"]]
            # One bounded selected source at a time; lock against correction
            # while storing its available original bytes. Never materialize a
            # portfolio/history worth of raw payloads in Python.
            row = db.scalar(select(model).where(model.id == entry["row_id"],
                model.tenant_id == sub.tenant_id).with_for_update())
            if row is None: raise ValueError("Selected invoice source disappeared before archival")
            observed = _fields(row, [k for k in values if hasattr(model,k)])
            expected = {k:v for k,v in values.items() if hasattr(model,k)}
            if _fingerprint([{"data":observed}]) != _fingerprint([{"data":expected}]):
                raise ValueError("Selected invoice source changed before archival")
            values = _fields(row)
        if entry["kind"] == "gmp_usage_raw" and not values.get("artifact_id"):
            # Legacy selected inline original: preserve once under a row lock.
            # This never clears inline data. Later invoices reuse its artifact.
            from ..source_artifacts import preserve_raw_version
            raw = db.scalar(select(GmpUsageRaw).where(GmpUsageRaw.id == entry["row_id"],
                GmpUsageRaw.tenant_id == sub.tenant_id).with_for_update())
            if raw is None: raise ValueError("Selected raw source disappeared before archival")
            observed = _fields(raw, RAW_META)
            if _fingerprint([{"data":observed}]) != _fingerprint([{"data":values}]):
                raise ValueError("Selected raw source changed before archival")
            if raw.artifact_id is None:
                preserve_raw_version(db, raw)
            values = dict(values, artifact_id=raw.artifact_id)
            db.expunge(raw)
            del raw
        data = save_values(values,entry["kind"],entry["row_id"])
        if entry["kind"] == "gmp_usage_raw" and values.get("artifact_id"):
            # Reuse a permanent object reference. Validation/decompression is
            # performed on download, not N times per invoice/retry.
            artifact = db.execute(select(SourceArtifact.id,SourceArtifact.sha256,
                SourceArtifact.byte_length,SourceArtifact.mime_type).where(
                SourceArtifact.id == values["artifact_id"],SourceArtifact.tenant_id == sub.tenant_id)).first()
            if artifact is None: raise ValueError("Selected raw source artifact unavailable")
            artifacts.append({"artifact_id":artifact.id,"sha256":artifact.sha256,
                "byte_length":artifact.byte_length,"mime_type":artifact.mime_type,
                "kind":entry["kind"],"row_id":entry["row_id"],"field":"raw_csv"})
            data["raw_csv"]={"artifact_id":artifact.id,"sha256":artifact.sha256}
        put(_json(data),entry["kind"],entry["row_id"])
        if entry.get("deferred"):
            db.expunge(row)  # do not retain source blobs until invoice commit
            del row
        del data, values
    put(_json(match.to_dict()), "normalized_calculation")
    limitations = [
        "Supporting records are bounded to the invoice period and candidate limits in capture_scope, not proven exhaustive per-field source lineage.",
        "Selected raw-window artifacts are linked without decompression; selected legacy inline originals are archived once. Unselected operational history remains retained.",
        "Metadata consistency checks cover selected numeric fields, identity, and available lengths; they do not prove byte equality for unversioned same-length source corrections.",
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
