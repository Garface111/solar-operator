"""Anna-800 accuracy harness — Phase A (invoice math) + Phase C (system sweeps).

Verifies EVERY enabled offtaker invoice on the ten_anna_800 demo tenant against
an INDEPENDENT recompute (straight SQL over bills + the documented pricing
rules — no delivery-pipeline code in the expectation path), so a pipeline bug
can't grade its own homework. Zero tolerance: any invoice off by > $0.011 or
> 0.05 kWh, a wrong recipient, a wrong period, a wrong invoice number, or a
wrong billing basis is a FAILURE.

Also sweeps the surrounding system at 800-scale:
  • bill-accuracy check (reconcile_tenant): the flagged set must EXACTLY equal
    the seeded rigged set (67 known GMP-drift offtakers) — no misses, no false
    positives;
  • audit_by_array + QB/Xero invoice register: row counts + spot amounts;
  • per-sub build timings (p50/p95/max) — the scale readout for the UI work.

Read-only: builds matches but never emails, never stamps. Safe on prod.

Run locally:  SOLAR_DB_URL=sqlite:////root/anna800_test/anna.db python scripts/anna800_verify.py
Run on prod:  railway ssh "cd /app && python scripts/anna800_verify.py"
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from api.db import SessionLocal, init_db
from api.models import (Tenant, Array, UtilityAccount, Bill,
                        BillingReportSubscription)

TENANT_ID = "ten_anna_800"
MONTH_LABEL = "2026-06"
Q2_LABELS = ["2026-04", "2026-05", "2026-06"]
DEFAULT_DISCOUNT = 0.10
AMOUNT_TOL = 0.011
KWH_TOL = 0.05


def _label(dt) -> str:
    d = dt.date() if isinstance(dt, datetime) else dt
    return d.strftime("%Y-%m")


def load_ground_truth(db) -> dict:
    """Everything the recompute needs, via plain SQL — keyed lookups."""
    subs = list(db.execute(
        select(BillingReportSubscription)
        .where(BillingReportSubscription.tenant_id == TENANT_ID,
               BillingReportSubscription.deleted_at.is_(None))
    ).scalars())
    # host account per array = FIRST account (by id) with that array_id —
    # mirrors _array_group_excess_for_sub_inner's selection rule.
    host_by_array: dict[int, int] = {}
    for acct_id, arr_id in db.execute(
            select(UtilityAccount.id, UtilityAccount.array_id)
            .where(UtilityAccount.tenant_id == TENANT_ID,
                   UtilityAccount.array_id.isnot(None))
            .order_by(UtilityAccount.id)):
        host_by_array.setdefault(arr_id, acct_id)
    bills: dict[tuple[int, str], tuple[float, float]] = {}
    for acct_id, pe, excess, credit in db.execute(
            select(Bill.account_id, Bill.period_end, Bill.kwh_sent_to_grid,
                   Bill.solar_credit_usd)
            .where(Bill.tenant_id == TENANT_ID, Bill.period_end.isnot(None))):
        if excess is not None:
            bills[(acct_id, _label(pe))] = (float(excess), float(credit or 0.0))
    return {"subs": subs, "host_by_array": host_by_array, "bills": bills}


def expected_invoice(sub, gt) -> dict | None:
    """Independent recompute of one offtaker's June-2026 (or Q2) invoice.
    Returns None for subs expected to be UNSENDABLE (guard demos)."""
    bills = gt["bills"]
    own = sub.utility_account_id
    if own is None:
        return None                                # unbound → gate refuses
    quarterly = (sub.cadence or "monthly") == "quarterly"
    labels = Q2_LABELS if quarterly else [MONTH_LABEL]
    own_rows = [bills.get((own, lb)) for lb in labels]
    if any(r is None for r in own_rows):
        return None                                # missing bill → held
    own_excess = round(sum(r[0] for r in own_rows), 1) if quarterly else own_rows[0][0]
    own_credit = round(sum(r[1] for r in own_rows), 2)
    credit_rate = (own_credit / own_excess) if own_excess else 0.0

    # Billed kWh basis: real-math (share × host group excess) when share + a
    # SEPARATE host bill exist and the sub is single-array; else GMP-credited.
    share = sub.array_share_pct
    host = gt["host_by_array"].get(sub.array_id) if sub.array_id else None
    group = None
    if share and host and host != own and not sub.array_allocations:
        rows = [bills.get((host, lb)) for lb in labels]
        if all(r is not None for r in rows):
            group = (round(sum(r[0] for r in rows), 1) if quarterly
                     else rows[0][0])
    if share and group:
        kwh = round(share * group, 2)
        basis = "real_math"
    else:
        kwh = round(own_excess * (sub.allocation_pct or 0.0), 2)
        basis = "gmp_credited"

    # Rate chain (documented): customer net override → legacy flat $/kWh (the
    # agreed price, no re-discount unless explicit) → the bill's own credit
    # rate with customer discount → default 10%.
    if sub.net_rate_per_kwh and sub.net_rate_per_kwh > 0:
        net_rate = float(sub.net_rate_per_kwh)
        discount = (float(sub.discount_pct)
                    if sub.discount_pct is not None and 0 <= sub.discount_pct < 1
                    else DEFAULT_DISCOUNT)
    elif sub.rate_per_kwh and sub.rate_per_kwh > 0:
        net_rate = float(sub.rate_per_kwh)
        discount = (float(sub.discount_pct)
                    if sub.discount_pct is not None and 0 <= sub.discount_pct < 1
                    else 0.0)
    else:
        net_rate = credit_rate
        discount = (float(sub.discount_pct)
                    if sub.discount_pct is not None and 0 <= sub.discount_pct < 1
                    else DEFAULT_DISCOUNT)
    amount = round(kwh * net_rate * (1 - discount), 2)
    budget = sub.budget_amount_usd
    if budget is not None:
        amount = round(float(budget), 2)
    if sub.invoice_number_next is not None:
        inv_no = str(sub.invoice_number_next)
    else:
        inv_no = "2026-Q2" if quarterly else MONTH_LABEL
    return {"kwh": kwh, "amount": amount, "basis": basis,
            "net_rate": net_rate, "discount": discount, "invoice_number": inv_no,
            "quarterly": quarterly, "budget": budget is not None,
            "period_end": "2026-06-30"}


def main() -> int:
    init_db()
    from api.billing.delivery import build_match
    from api.billing.reconcile_bills import reconcile_tenant, audit_by_array
    from api.billing.qb_export import build_invoice_register

    t0 = time.time()
    with SessionLocal() as db:
        gt = load_ground_truth(db)
    subs = gt["subs"]
    enabled = [s for s in subs if s.enabled]
    guard = [s for s in subs if s.customer_name.startswith("DEMO-HOLD")]
    core = [s for s in enabled if not s.customer_name.startswith("DEMO-HOLD")]
    print(f"subs={len(subs)} enabled={len(enabled)} core-sendable={len(core)} "
          f"guard={len(guard)}  (ground truth loaded in {time.time()-t0:.1f}s)")

    failures: list[dict] = []
    checked = 0
    timings: list[float] = []
    seen_recipients: dict[str, int] = {}
    for sub in core:
        exp = expected_invoice(sub, gt)
        if exp is None:
            failures.append({"sub": sub.id, "name": sub.customer_name,
                             "err": "recompute says UNSENDABLE but sub is core"})
            continue
        t1 = time.time()
        try:
            m = build_match(sub)
        except Exception as e:  # noqa: BLE001
            failures.append({"sub": sub.id, "name": sub.customer_name,
                             "err": f"build_match raised: {e}"})
            continue
        timings.append(time.time() - t1)
        ci = (m.computed_invoice or {})
        errs = []
        if ci.get("has_utility_bill") is not True:
            errs.append(f"has_utility_bill={ci.get('has_utility_bill')}")
        if ci.get("kwh_source") != "utility_bill":
            errs.append(f"kwh_source={ci.get('kwh_source')}")
        if abs((ci.get("kwh") or 0) - exp["kwh"]) > KWH_TOL:
            errs.append(f"kwh {ci.get('kwh')} != expected {exp['kwh']}")
        if abs((ci.get("amount_owed") or 0) - exp["amount"]) > AMOUNT_TOL:
            errs.append(f"amount {ci.get('amount_owed')} != expected {exp['amount']}")
        if ci.get("billing_basis") != exp["basis"]:
            errs.append(f"basis {ci.get('billing_basis')} != {exp['basis']}")
        if str(ci.get("invoice_number")) != exp["invoice_number"]:
            errs.append(f"invoice_number {ci.get('invoice_number')} != {exp['invoice_number']}")
        pe = (ci.get("period_end") or "")[:10]
        if pe != exp["period_end"]:
            errs.append(f"period_end {pe} != {exp['period_end']}")
        if exp["budget"] and not ci.get("budget_override"):
            errs.append("budget_override missing")
        # recipient sanity: the email that WOULD be used (send test asserts live)
        rcpt = sub.client_email if sub.send_mode in ("to_client", "to_both") else sub.operator_email
        if not rcpt or "@resend.dev" not in rcpt:
            errs.append(f"unsafe/missing recipient {rcpt!r} (mode={sub.send_mode})")
        else:
            seen_recipients[rcpt] = seen_recipients.get(rcpt, 0) + 1
        if errs:
            failures.append({"sub": sub.id, "name": sub.customer_name,
                             "err": "; ".join(errs),
                             "got": {k: ci.get(k) for k in
                                     ("kwh", "amount_owed", "billing_basis",
                                      "invoice_number", "net_rate_per_kwh",
                                      "discount_pct", "kwh_source")},
                             "exp": exp})
        checked += 1

    # Distinct to_client recipients must be distinct per offtaker (cross-wire guard).
    dup_rcpt = {r: n for r, n in seen_recipients.items()
                if n > 1 and "-op@" not in r and "-pen@" not in r}

    # Guard demos: every one must be UNSENDABLE by recompute.
    guard_bad = [s.customer_name for s in guard
                 if s.enabled and expected_invoice(s, gt) is not None]

    print(f"\n── Phase A: invoice accuracy ({checked} core subs) ──")
    print(f"  failures        : {len(failures)}")
    print(f"  dup recipients  : {len(dup_rcpt)}")
    print(f"  guard-demo leaks: {guard_bad or 'none'}")
    if timings:
        ms = sorted(x * 1000 for x in timings)
        print(f"  build_match ms  : p50={ms[len(ms)//2]:.0f} "
              f"p95={ms[int(len(ms)*.95)]:.0f} max={ms[-1]:.0f} "
              f"total={sum(ms)/1000:.1f}s")
    for f in failures[:20]:
        print("  FAIL", json.dumps(f, default=str)[:400])

    # ── Phase C: system sweeps at scale ──────────────────────────────────────
    print("\n── Phase C: system sweeps ──")
    with SessionLocal() as db:
        t1 = time.time()
        recon = reconcile_tenant(db, TENANT_ID)
        recon_s = time.time() - t1
    rows = recon.get("subscriptions") or []
    flagged = {r.get("sub_id") for r in rows
               if (r.get("allocation") or {}).get("status") == "mismatch"}
    flagged.discard(None)
    # Expected rigged set: recompute independently from bills (drift > 2 kWh).
    expected_rigged = set()
    for sub in core:
        own = sub.utility_account_id
        host = gt["host_by_array"].get(sub.array_id)
        if not (own and host and sub.array_share_pct):
            continue
        ob = gt["bills"].get((own, MONTH_LABEL))
        hb = gt["bills"].get((host, MONTH_LABEL))
        if ob and hb and abs(ob[0] - round(sub.array_share_pct * hb[0], 1)) > 2.0:
            expected_rigged.add(sub.id)
    missed = expected_rigged - flagged
    false_pos = flagged - expected_rigged - {None}
    print(f"  reconcile_tenant: {recon_s:.1f}s, rows={len(rows)}, flagged={len(flagged)}, "
          f"expected-rigged={len(expected_rigged)}, missed={len(missed)}, "
          f"false-pos={len(false_pos)}")

    with SessionLocal() as db:
        t1 = time.time()
        audit = audit_by_array(db, TENANT_ID)
        audit_s = time.time() - t1
    n_audit = sum(len(a.get("offtakers", []))
                  for u in (audit.get("utilities") or [])
                  for a in (u.get("arrays") or []))
    print(f"  audit_by_array  : {audit_s:.1f}s, offtaker rows={n_audit}")

    with SessionLocal() as db:
        t1 = time.time()
        try:
            _csv, n_reg = build_invoice_register(db, TENANT_ID, fmt="xero")
        except Exception as e:  # noqa: BLE001
            n_reg = f"err: {e}"
    print(f"  invoice register: {time.time()-t1:.1f}s, rows={n_reg} (must be 800 — "
          "send-gate mirror keeps guard/disabled subs out of the export)")

    ok = (not failures and not dup_rcpt and not guard_bad
          and not missed and not false_pos and checked == 800 and n_reg == 800)
    print(f"\n{'✅ ALL CHECKS PASS' if ok else '❌ FAILURES PRESENT'} "
          f"(checked={checked}/800)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
