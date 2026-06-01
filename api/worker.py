"""
Worker — does the actual scraping work the API queues up.

Two main entry points:
  pull_bills_for_tenant(tenant_id) -> pulls every enabled account's current bill
  run_pending_jobs()              -> walks the Job table and dispatches

The scheduler in scheduler.py calls these on a cadence (or you can hit them
via /admin/jobs/run in the API).
"""
from __future__ import annotations
import pathlib, traceback
from datetime import datetime
from sqlalchemy import select
from .db import SessionLocal, DATA_DIR
from .models import Tenant, UtilityAccount, UtilitySession, Bill, Job, now
from .adapters import get_adapter


BILLS_DIR = DATA_DIR / "bills"
BILLS_DIR.mkdir(exist_ok=True, parents=True)


def pull_bills_for_tenant(tenant_id: str) -> dict:
    """Pull current bill for every enabled account for one tenant.
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
            current_bill_url = (acc.extra or {}).get("currentBillUrlBinary") or \
                               (acc.extra or {}).get("current_bill_url")
            # We stored the captured currentBillUrl under acc.extra (see api.py).
            if not current_bill_url:
                results.append({
                    "account": acc.account_number, "nickname": acc.nickname,
                    "status": "skipped", "reason": "no current_bill_url on file",
                })
                continue

            adapter = get_adapter(acc.provider)
            ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
            safe = (acc.nickname or acc.account_number).replace(" ", "_").replace("/", "_")
            pdf_path = BILLS_DIR / tenant_id / f"{ts}_{acc.provider}_{safe}.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                adapter.fetch_bill_pdf(current_bill_url, pdf_path)
                metrics = adapter.extract_bill_metrics(pdf_path)
            except Exception as e:
                results.append({
                    "account": acc.account_number, "nickname": acc.nickname,
                    "status": "failed", "error": str(e),
                    "trace": traceback.format_exc(limit=2),
                })
                continue

            doc_number = (acc.extra or {}).get("documentNumber")  # may be None
            # De-dupe by (account_id, period_end + bill_date)
            existing = None
            if metrics["period_end"]:
                existing = db.execute(
                    select(Bill).where(
                        Bill.account_id == acc.id,
                        Bill.period_end == metrics["period_end"],
                    )
                ).scalar_one_or_none()

            if existing:
                existing.kwh_generated = metrics["kwh_generated"]
                existing.billing_days  = metrics["billing_days"]
                existing.period_start  = metrics["period_start"]
                existing.bill_date     = metrics["bill_date"]
                existing.pdf_path      = str(pdf_path)
                existing.raw_text      = metrics["raw_text"]
                existing.parse_status  = metrics["parse_status"]
                existing.pulled_at     = now()
                bill_row = existing
                action = "updated"
            else:
                bill_row = Bill(
                    tenant_id=tenant_id, account_id=acc.id,
                    bill_date=metrics["bill_date"],
                    period_start=metrics["period_start"],
                    period_end=metrics["period_end"],
                    billing_days=metrics["billing_days"],
                    kwh_generated=metrics["kwh_generated"],
                    pdf_path=str(pdf_path),
                    raw_text=metrics["raw_text"],
                    parse_status=metrics["parse_status"],
                    document_number=doc_number,
                )
                db.add(bill_row)
                action = "created"

            db.flush()
            results.append({
                "account": acc.account_number, "nickname": acc.nickname,
                "status": "ok", "action": action,
                "kwh_generated": metrics["kwh_generated"],
                "billing_days": metrics["billing_days"],
                "period": (
                    metrics["period_start"].strftime("%Y-%m-%d") if metrics["period_start"] else None,
                    metrics["period_end"].strftime("%Y-%m-%d") if metrics["period_end"] else None,
                ),
                "pdf": str(pdf_path.name),
            })

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
