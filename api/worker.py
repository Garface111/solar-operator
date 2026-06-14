"""
Worker — does the actual scraping work the API queues up.

Two main entry points:
  pull_bills_for_tenant(tenant_id) -> pulls every enabled account's full bill
                                      history (JSON-first, PDF fallback)
  run_pending_jobs()              -> walks the Job table and dispatches

The scheduler in scheduler.py calls these on a cadence (or you can hit them
via /admin/jobs/run in the API).

Strategy (per account):
  1. PREFERRED: GET https://api.greenmountainpower.com/api/v2/accounts/{n}/bills
     using the stored JWT. Returns full history; we upsert every bill in one
     pass. Robust against PDF format changes.
  2. FALLBACK: the old per-account currentBillUrl → Utilitec redirector → PDF
     parse path. Only used if the JSON call raises (expired JWT, GMP API
     downtime, schema change).
"""
from __future__ import annotations
import pathlib, traceback
from datetime import datetime
from sqlalchemy import select
from .db import SessionLocal, DATA_DIR
from .models import Tenant, UtilityAccount, UtilitySession, Bill, Job, now
from .adapters import get_adapter
from .sessions import token_for_account


BILLS_DIR = DATA_DIR / "bills"
BILLS_DIR.mkdir(exist_ok=True, parents=True)


def _latest_session_token(db, tenant_id: str, provider: str) -> str | None:
    sess = db.execute(
        select(UtilitySession)
        .where(UtilitySession.tenant_id == tenant_id,
               UtilitySession.provider == provider)
        .order_by(UtilitySession.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return sess.api_token if sess else None


def _upsert_bill(db, tenant_id: str, account: UtilityAccount,
                 metrics: dict, source_path: str | None = None) -> str:
    """Upsert one bill row. Returns 'created' or 'updated'."""
    existing = None
    if metrics.get("period_end"):
        existing = db.execute(
            select(Bill).where(
                Bill.account_id == account.id,
                Bill.period_end == metrics["period_end"],
            )
        ).scalar_one_or_none()

    if existing:
        existing.kwh_generated = metrics["kwh_generated"]
        existing.billing_days  = metrics["billing_days"]
        existing.period_start  = metrics["period_start"]
        existing.bill_date     = metrics["bill_date"]
        existing.raw_text      = metrics.get("raw_text", "")
        existing.parse_status  = metrics["parse_status"]
        existing.pulled_at     = now()
        if source_path:
            existing.pdf_path = source_path
        if metrics.get("document_number"):
            existing.document_number = metrics["document_number"]
        return "updated"

    db.add(Bill(
        tenant_id=tenant_id, account_id=account.id,
        bill_date=metrics["bill_date"],
        period_start=metrics["period_start"],
        period_end=metrics["period_end"],
        billing_days=metrics["billing_days"],
        kwh_generated=metrics["kwh_generated"],
        pdf_path=source_path,
        raw_text=metrics.get("raw_text", ""),
        parse_status=metrics["parse_status"],
        document_number=metrics.get("document_number"),
    ))
    return "created"


def _pull_via_json(db, tenant_id: str, account: UtilityAccount,
                   adapter, jwt: str) -> dict:
    """JSON-first path. Returns result dict."""
    bills = adapter.fetch_bills_json(account.account_number, jwt)
    created = updated = 0
    skipped_no_kwh = 0
    for b in bills:
        metrics = adapter.bill_json_to_metrics(b)
        if metrics["kwh_generated"] is None or metrics["kwh_generated"] <= 0:
            skipped_no_kwh += 1
            continue
        action = _upsert_bill(db, tenant_id, account, metrics, source_path=None)
        if action == "created":
            created += 1
        else:
            updated += 1
    return {
        "account": account.account_number, "nickname": account.nickname,
        "status": "ok", "source": "json",
        "bills_returned": len(bills),
        "created": created, "updated": updated,
        "skipped_no_kwh": skipped_no_kwh,
    }


def _pull_via_pdf(db, tenant_id: str, account: UtilityAccount, adapter) -> dict:
    """Fallback PDF-redirector path. Pulls only the CURRENT bill (the only
    one currentBillUrl points to), parses it, upserts one row."""
    current_bill_url = (account.extra or {}).get("currentBillUrlBinary") or \
                       (account.extra or {}).get("current_bill_url")
    if not current_bill_url:
        return {"account": account.account_number, "nickname": account.nickname,
                "status": "skipped", "reason": "no current_bill_url"}

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    safe = (account.nickname or account.account_number).replace(" ", "_").replace("/", "_")
    pdf_path = BILLS_DIR / tenant_id / f"{ts}_{account.provider}_{safe}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    adapter.fetch_bill_pdf(current_bill_url, pdf_path)
    metrics = adapter.extract_bill_metrics(pdf_path)
    metrics["source"] = "pdf"
    action = _upsert_bill(db, tenant_id, account, metrics,
                          source_path=str(pdf_path))
    return {
        "account": account.account_number, "nickname": account.nickname,
        "status": "ok", "source": "pdf", "action": action,
        "kwh_generated": metrics["kwh_generated"],
        "billing_days": metrics["billing_days"],
        "pdf": pdf_path.name,
    }


def pull_bills_for_tenant(tenant_id: str) -> dict:
    """Pull all available bills for every enabled account for one tenant.
    Returns a per-account result summary."""
    results: list[dict] = []
    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_id)
        if not tenant:
            return {"error": f"unknown tenant {tenant_id}"}

        accounts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant_id,
                UtilityAccount.enabled == True,
            )
        ).scalars().all()

        for acc in accounts:
            adapter = get_adapter(acc.provider)
            # Pick the token bound to THIS account's login identity, not just
            # the tenant's latest capture — so every client's login keeps
            # scraping even after the operator logs in as a different client.
            jwt = token_for_account(db, acc)

            # Try JSON path first
            json_attempted = False
            if jwt and hasattr(adapter, "fetch_bills_json"):
                json_attempted = True
                try:
                    results.append(_pull_via_json(db, tenant_id, acc, adapter, jwt))
                    continue
                except Exception as e:
                    json_err = f"{type(e).__name__}: {e}"

            # Fallback: PDF
            try:
                r = _pull_via_pdf(db, tenant_id, acc, adapter)
                if json_attempted:
                    r["json_fallback_reason"] = json_err
                results.append(r)
            except Exception as e:
                results.append({
                    "account": acc.account_number, "nickname": acc.nickname,
                    "status": "failed",
                    "json_error": json_err if json_attempted else None,
                    "pdf_error": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc(limit=2),
                })

        # Stamp last_pull_at so the dashboard can show the next-pull countdown.
        tenant.last_pull_at = now()
        db.commit()

    return {
        "tenant": tenant_id,
        "ran_at": datetime.utcnow().isoformat() + "Z",
        "accounts_processed": len(results),
        "results": results,
    }


def run_job(job_id: int) -> dict:
    """Execute one queued Job row."""
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return {"error": f"unknown job {job_id}"}
        if job.status != "queued":
            return {"error": f"job {job_id} not queued (status={job.status})"}
        job.status = "running"
        job.started_at = now()
        db.commit()

        try:
            if job.kind == "pull_bills":
                result = pull_bills_for_tenant(job.tenant_id)
            else:
                raise ValueError(f"unknown job kind: {job.kind}")
            job.status = "succeeded"
            job.result = result
        except Exception as e:
            job.status = "failed"
            job.error = f"{e}\n{traceback.format_exc(limit=4)}"
        finally:
            job.finished_at = now()
            db.commit()
            return {"job_id": job_id, "status": job.status, "result": job.result, "error": job.error}


def run_pending_jobs(limit: int = 20) -> list[dict]:
    out = []
    with SessionLocal() as db:
        pending = db.execute(
            select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(limit)
        ).scalars().all()
    for j in pending:
        out.append(run_job(j.id))
    return out
