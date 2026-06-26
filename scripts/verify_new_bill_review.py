"""Local verification for api/jobs/new_bill_review (the come-review-your-next-bill
email). Seeds a temp SQLite DB with a tenant + GMP utility account + a settled
bill (excess sent to grid + a cashed solar credit) + a bound offtaker
subscription, then exercises the job:

  1. DRY-RUN → prints recipient resolution, subject, period, amount, and the
     rendered HTML (link + CTA) WITHOUT sending or stamping dedup.
  2. DEDUP   → run again live (no Resend key → notify logs instead of sends);
     confirm it stamps review_emailed_period and won't re-fire for the same period.
  3. NEW BILL → land a newer bill period; confirm the job fires again.

Run:  DATABASE_URL=sqlite:////tmp/nbr_verify.db python -m scripts.verify_new_bill_review
Prints to stderr (so a railway-ssh wrapper's grep can capture it).
"""
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/nbr_verify.db")

# Fresh DB each run.
_p = os.environ["DATABASE_URL"].replace("sqlite:///", "")
if _p.startswith("/") and os.path.exists(_p):
    os.remove(_p)

from api.db import SessionLocal, engine  # noqa: E402
from api.models import (  # noqa: E402
    Base, Tenant, UtilityAccount, Bill, BillingReportSubscription,
)

Base.metadata.create_all(engine)


def log(*a):
    print(*a, file=sys.stderr)


def seed():
    with SessionLocal() as db:
        t = Tenant(
            id="ten_verify_nbr01", name="Verify Solar Co", company_name="Verify Solar Co",
            tenant_key="tk_verify_nbr01",
            contact_email="operator@verifysolar.test", product="array_operator",
            active=True, subscription_status="comped",
        )
        db.add(t); db.flush()
        acct = UtilityAccount(
            tenant_id=t.id, provider="gmp", account_number="9999000000",
            nickname="Maple Lane Array",
        )
        db.add(acct); db.flush()
        # A settled bill: excess sent to grid + a cashed solar credit → billable.
        pe = datetime(2026, 5, 24)
        db.add(Bill(
            tenant_id=t.id, account_id=acct.id,
            bill_date=datetime(2026, 5, 25),
            period_start=datetime(2026, 4, 25), period_end=pe,
            kwh_generated=4200, kwh_consumed=900,
            kwh_sent_to_grid=3300.0, solar_credit_usd=560.0,
            parse_status="parsed",
        ))
        sub = BillingReportSubscription(
            tenant_id=t.id, customer_name="Maple Lane Offtaker",
            utility_account_id=acct.id, allocation_pct=1.0,
            cadence="monthly", delivery_mode="approval",
            send_mode="to_me", operator_email="operator@verifysolar.test",
            enabled=True,
        )
        db.add(sub); db.commit()
        return sub.id


def main():
    from api.jobs.new_bill_review import run_new_bill_reviews
    from api.models import ReportDraft

    # Stub the Resend transport so the dedup/stamp logic is exercised
    # deterministically (no live key on this box). Capture each send's recipient
    # + subject so we can confirm the OPERATOR is the target. The dry-run path
    # never calls this — it must stay at 0.
    import api.notify as notify
    sent_log = []
    _orig = notify._send_via_resend

    def _capture(*, to, subject, html=None, text=None, **kw):  # noqa: ANN001
        sent_log.append({"to": to, "subject": subject, "from_addr": kw.get("from_addr"),
                         "product": kw.get("product")})
        return True
    notify._send_via_resend = _capture

    sub_id = seed()
    log("=" * 70)
    log("SEED: 1 AO offtaker sub bound to GMP acct, 1 settled bill (period 2026-05)")
    log("=" * 70)

    # 1. DRY-RUN -----------------------------------------------------------------
    res = run_new_bill_reviews(dry_run=True)
    log("\n--- 1. DRY-RUN ---")
    log("candidates:", len(res["candidates"]), "| emailed:", res["emailed"],
        "(must be 0 — dry run sends nothing)")
    for p in res["previews"]:
        log("  recipient (OPERATOR):", p["recipient"])
        log("  subject:             ", p["subject"])
        log("  bill period:         ", p["period"])
        log("  draft period span:   ", p["draft_period_label"])
        log("  amount_usd:          ", p["amount_usd"])
        log("  customer_kwh:        ", p["customer_kwh"])
        html = p["html"]
        log("  link present (#reports):", "#reports" in html)
        log("  CTA 'Review & send':    ", "Review &amp; send" in html or "Review & send" in p["text"])
        log("  AO blue accent #2563eb: ", "#2563eb" in html)
        log("  no AI mention:          ", ("ai" not in p["text"].lower().replace("email", "")
                                           .replace("available", "").replace("detail", "")))
        # dump a slice so we can eyeball the body
        i = html.find("A new utility bill")
        log("  body snippet:           ", html[i:i + 180].replace("\n", " ") if i >= 0 else "(not found)")
    # confirm dry-run persisted NOTHING
    with SessionLocal() as db:
        drafts = db.query(ReportDraft).count()
        sub = db.get(BillingReportSubscription, sub_id)
        log("  drafts in DB after dry-run:", drafts, "(must be 0)")
        log("  review_emailed_period:    ", repr(sub.review_emailed_period), "(must be None)")

    # 2. LIVE run (no Resend key → notify LOGS the send) --------------------------
    log("\n--- 2. LIVE run #1 (no RESEND_API_KEY → notify logs the email) ---")
    res2 = run_new_bill_reviews()
    log("emailed:", res2["emailed"], "| candidates:", len(res2["candidates"]))
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sub_id)
        drafts = db.query(ReportDraft).count()
        log("  review_emailed_period now:", repr(sub.review_emailed_period),
            "(must be '2026-05')")
        log("  drafts in DB:             ", drafts, "(must be 1 — the invoice draft)")

    # 3. DEDUP: run again, same bill → must NOT re-fire -------------------------
    log("\n--- 3. LIVE run #2, same bill (DEDUP) ---")
    res3 = run_new_bill_reviews()
    log("emailed:", res3["emailed"], "(must be 0 — already prompted for 2026-05)")

    # 4. NEW BILL lands → must fire again ----------------------------------------
    log("\n--- 4. A NEWER bill period lands ---")
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sub_id)
        db.add(Bill(
            tenant_id=sub.tenant_id, account_id=sub.utility_account_id,
            bill_date=datetime(2026, 6, 25),
            period_start=datetime(2026, 5, 25), period_end=datetime(2026, 6, 24),
            kwh_generated=5100, kwh_consumed=850,
            kwh_sent_to_grid=4200.0, solar_credit_usd=720.0,
            parse_status="parsed",
        ))
        db.commit()
    res4 = run_new_bill_reviews()
    log("emailed:", res4["emailed"], "(must be 1 — new period 2026-06)")
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sub_id)
        log("  review_emailed_period now:", repr(sub.review_emailed_period),
            "(must be '2026-06')")

    log("\n--- send log (every actual send target) ---")
    for s in sent_log:
        log("  →", s["to"], "|", s["subject"], "| product:", s["product"])
    operator_only = all(s["to"] == "operator@verifysolar.test" for s in sent_log)

    log("\n" + "=" * 70)
    ok = (res["emailed"] == 0 and res2["emailed"] == 1 and res3["emailed"] == 0
          and res4["emailed"] == 1 and len(sent_log) == 2 and operator_only)
    log("dry-run sent nothing:        ", res["emailed"] == 0)
    log("new bill → 1 send to operator:", res2["emailed"] == 1 and sent_log and sent_log[0]["to"] == "operator@verifysolar.test")
    log("dedup → no re-send:          ", res3["emailed"] == 0)
    log("newer bill → fires again:    ", res4["emailed"] == 1)
    log("every send → OPERATOR only:  ", operator_only)
    notify._send_via_resend = _orig
    log("VERIFY RESULT:", "ALL PASS ✅" if ok else "FAIL ❌")
    log("=" * 70)


if __name__ == "__main__":
    main()
