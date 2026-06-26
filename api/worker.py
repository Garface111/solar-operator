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
                 metrics: dict, source_path: str | None = None,
                 pdf_bytes: bytes | None = None,
                 pdf_content_type: str | None = None) -> str:
    """Upsert one bill row. Returns 'created' or 'updated'.

    `pdf_bytes` (+ content type) are the DURABLE bill-PDF bytes — persisted
    in-row so the auto-attach-GMP-bill feature can read them after a redeploy
    (pdf_path alone is ephemeral). Optional; null when the PDF wasn't captured.
    """
    existing = None
    if metrics.get("period_end"):
        # Tolerate DUPLICATE bill rows for the same (account, period_end). Some accounts
        # have accumulated hundreds-to-thousands from capture sprawl (Rick Lunt's had
        # 1001), and scalar_one_or_none() then RAISED MultipleResultsFound — which crashed
        # the WHOLE pull, so no new bill ever landed server-side (the real "GMP bill didn't
        # update" cause). Update the most-recent matching row instead of crashing; the
        # offtaker invoice selects by period_end DESC so the freshest row wins.
        # (Deduping the historical rows is a separate cleanup.)
        existing = db.execute(
            select(Bill).where(
                Bill.account_id == account.id,
                Bill.period_end == metrics["period_end"],
            ).order_by(Bill.id.desc())
        ).scalars().first()

    # Full energy-record fields — present on the JSON path (the sponge), absent
    # on the legacy PDF path (.get → None, columns nullable). raw_json is the
    # authoritative full bill so nothing GMP exposed is ever lost.
    _sponge = dict(
        kwh_consumed=metrics.get("kwh_consumed"),
        kwh_sent_to_grid=metrics.get("kwh_sent_to_grid"),
        kwh_gross_generated=metrics.get("kwh_gross_generated"),
        is_net_metered=metrics.get("is_net_metered"),
        total_cost=metrics.get("total_cost"),
        net_credit=metrics.get("net_credit"),
        solar_credit_usd=metrics.get("solar_credit_usd"),
        avg_rate_cents_kwh=metrics.get("avg_rate_cents_kwh"),
        supplier=metrics.get("supplier"),
        raw_json=metrics.get("raw_json"),
    )

    if existing:
        existing.kwh_generated = metrics["kwh_generated"]
        existing.billing_days  = metrics["billing_days"]
        existing.period_start  = metrics["period_start"]
        existing.bill_date     = metrics["bill_date"]
        existing.raw_text      = metrics.get("raw_text", "")
        existing.parse_status  = metrics["parse_status"]
        existing.pulled_at     = now()
        for k, v in _sponge.items():
            if v is not None:          # never overwrite a known value with None
                setattr(existing, k, v)
        if source_path:
            existing.pdf_path = source_path
        if pdf_bytes:
            existing.pdf_bytes = pdf_bytes
            existing.pdf_content_type = pdf_content_type or "application/pdf"
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
        pdf_bytes=pdf_bytes,
        pdf_content_type=(pdf_content_type or "application/pdf") if pdf_bytes else None,
        raw_text=metrics.get("raw_text", ""),
        parse_status=metrics["parse_status"],
        document_number=metrics.get("document_number"),
        **_sponge,
    ))
    return "created"


def _pull_via_json(db, tenant_id: str, account: UtilityAccount,
                   adapter, jwt: str) -> dict:
    """JSON-first path. Returns result dict."""
    bills = adapter.fetch_bills_json(account.account_number, jwt)
    created = updated = 0
    no_generation = 0
    for b in bills:
        metrics = adapter.bill_json_to_metrics(b)
        # DATA SPONGE: absorb EVERY bill into the energy record — consumption,
        # cost, rate, net credits — not just solar-generation periods. (Previously
        # we skipped any bill with no kWh generated, throwing away the rest of the
        # owner's energy history.) We still TRACK the no-generation count so the
        # NEPOOL generation signal is observable, but we persist the full record.
        if metrics["kwh_generated"] is None or metrics["kwh_generated"] <= 0:
            no_generation += 1
        action = _upsert_bill(db, tenant_id, account, metrics, source_path=None)
        if action == "created":
            created += 1
        else:
            updated += 1
    # The JSON path persists bill METRICS for all history but no PDF. Capture
    # the CURRENT bill's PDF bytes durably onto its bill row so auto-attach has
    # the real utility PDF for the latest period. Best-effort: never fail the
    # pull over a PDF fetch (auth/format issues surface in the result).
    pdf_capture = _capture_current_bill_pdf(db, tenant_id, account, adapter, jwt)
    return {
        "account": account.account_number, "nickname": account.nickname,
        "status": "ok", "source": "json",
        "bills_returned": len(bills),
        "created": created, "updated": updated,
        "no_generation": no_generation,
        "pdf_capture": pdf_capture,
    }


def _capture_current_bill_pdf(db, tenant_id: str, account: UtilityAccount,
                              adapter, jwt: str | None = None) -> dict:
    """Persist the CURRENT bill's PDF bytes onto its bill row, for auto-attach.

    PRIMARY (works for EVERY account, including the managed-customer / offtaker
    accounts whose extension capture never grabbed a per-account currentBillUrl):
    the GMP /transactions endpoint hands a per-bill PDF link (`urlBinary`) for any
    account the operator's JWT can see. FALLBACK: the account's own stored
    currentBillUrl (present only for the operator's primary accounts). Best-effort
    — never raises into the pull. Returns a small status dict."""
    from datetime import timedelta
    # Newest bill row for this account is where the current PDF belongs.
    bill = db.execute(
        select(Bill).where(Bill.account_id == account.id)
        .order_by(Bill.period_end.desc().nullslast(), Bill.bill_date.desc().nullslast())
    ).scalars().first()
    if bill is None:
        return {"saved": False, "reason": "no bill row to attach to"}
    if bill.pdf_bytes:
        return {"saved": False, "reason": "already captured", "bill_id": bill.id}

    # ── PRIMARY: transactions → urlBinary (works for managed/offtaker accounts) ──
    if jwt and hasattr(adapter, "fetch_transactions") and hasattr(adapter, "fetch_bill_pdf_binary"):
        try:
            txns = adapter.fetch_transactions(
                account.account_number, jwt,
                datetime.utcnow() - timedelta(days=400), datetime.utcnow() + timedelta(days=1))
            docs = sorted(
                [t for t in txns if isinstance(t, dict) and t.get("urlBinary")],
                key=lambda t: t.get("date") or "", reverse=True)
            if docs:
                # Pick the statement doc aligned to THIS bill's period, not blindly the
                # newest transaction — a newer unrelated doc (payment, adjustment) must
                # not put the wrong month's PDF on the row. Closest by date to the bill's
                # own date; fall back to the newest doc when nothing parses.
                target = bill.bill_date or bill.period_end
                chosen = docs[0]
                if target is not None:
                    def _gap(t):
                        ds = str(t.get("date") or "")[:10]
                        try:
                            y, mo, dy = int(ds[0:4]), int(ds[5:7]), int(ds[8:10])
                            return abs((datetime(y, mo, dy).date() - target.date()).days)
                        except Exception:
                            return 10 ** 6
                    chosen = min(docs, key=_gap)
                data, ctype = adapter.fetch_bill_pdf_binary(chosen["urlBinary"])
                bill.pdf_bytes = data
                bill.pdf_content_type = ctype or "application/pdf"
                return {"saved": True, "bill_id": bill.id, "bytes": len(data),
                        "via": "transactions", "doc_date": chosen.get("date")}
        except Exception:  # noqa: BLE001 — fall through to the legacy currentBillUrl path
            pass

    # ── FALLBACK: the account's own currentBillUrl (operator's primary accounts) ──
    current_bill_url = (account.extra or {}).get("currentBillUrlBinary") or \
                       (account.extra or {}).get("current_bill_url")
    if not current_bill_url:
        return {"saved": False, "reason": "no transactions doc and no current_bill_url"}
    if not hasattr(adapter, "fetch_bill_pdf"):
        return {"saved": False, "reason": "adapter has no fetch_bill_pdf"}
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    safe = (account.nickname or account.account_number).replace(" ", "_").replace("/", "_")
    pdf_path = BILLS_DIR / tenant_id / f"{ts}_{account.provider}_{safe}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _, content_type = adapter.fetch_bill_pdf(current_bill_url, pdf_path)
        data = pdf_path.read_bytes()
    except Exception as e:  # noqa: BLE001 — auth/format/transport; surface, don't fail the pull
        return {"saved": False, "reason": f"fetch failed: {type(e).__name__}: {e}"}
    if not data or data[:4] != b"%PDF":
        return {"saved": False, "reason": "not a PDF (auth redirect?)"}
    bill.pdf_bytes = data
    bill.pdf_content_type = content_type or "application/pdf"
    bill.pdf_path = str(pdf_path)
    return {"saved": True, "bill_id": bill.id, "bytes": len(data), "via": "currentBillUrl"}


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

    path, content_type = adapter.fetch_bill_pdf(current_bill_url, pdf_path)
    metrics = adapter.extract_bill_metrics(pdf_path)
    metrics["source"] = "pdf"
    # Persist the actual PDF bytes durably (in-row) — pdf_path is ephemeral on
    # Railway, and the auto-attach-GMP-bill feature reads these bytes later.
    try:
        pdf_bytes = pathlib.Path(pdf_path).read_bytes()
    except OSError:
        pdf_bytes = None
    action = _upsert_bill(db, tenant_id, account, metrics,
                          source_path=str(pdf_path),
                          pdf_bytes=pdf_bytes,
                          pdf_content_type=content_type or "application/pdf")
    return {
        "account": account.account_number, "nickname": account.nickname,
        "status": "ok", "source": "pdf", "action": action,
        "kwh_generated": metrics["kwh_generated"],
        "billing_days": metrics["billing_days"],
        "pdf": pdf_path.name,
        "pdf_bytes_saved": bool(pdf_bytes),
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


def pull_account_bills(tenant_id: str, account_id: int) -> dict:
    """Pull bills for ONE account on demand — e.g. right before generating an offtaker
    invoice, so it reflects the LATEST GMP statement without waiting for the 6h
    scheduler. Same JSON-first / PDF-fallback path as the full tenant pull. Commits, so
    the caller's next (READ COMMITTED) query sees the fresh bill."""
    with SessionLocal() as db:
        acc = db.get(UtilityAccount, account_id)
        if not acc or acc.tenant_id != tenant_id or getattr(acc, "enabled", True) is False:
            return {"status": "skipped", "reason": "account-not-eligible"}
        adapter = get_adapter(acc.provider)
        jwt = token_for_account(db, acc)
        json_err = None
        if jwt and hasattr(adapter, "fetch_bills_json"):
            try:
                r = _pull_via_json(db, tenant_id, acc, adapter, jwt)
                db.commit()
                return r
            except Exception as e:  # noqa: BLE001
                json_err = f"{type(e).__name__}: {e}"
        try:
            r = _pull_via_pdf(db, tenant_id, acc, adapter)
            db.commit()
            if json_err:
                r["json_fallback_reason"] = json_err
            return r
        except Exception as e:  # noqa: BLE001
            db.rollback()
            return {"status": "failed", "json_error": json_err,
                    "pdf_error": f"{type(e).__name__}: {e}"}


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
