"""
Billing-report delivery — the shared pipeline used by BOTH the "send now"
endpoint and the scheduler.

deliver_subscription():
  1. rebuild the BillingMatch from the subscription's stored workbook bytes,
  2. generate the chosen attachments (invoice PDF/XLSX + optional summary),
  3. resolve recipients from the send-mode slider (to me / to client / to both),
  4. send one branded Array Operator email via Resend,
  5. stamp last_sent_at / next_send_at / last_invoice_number.

Keeping this here (not in routes) means the scheduler can import it without
pulling FastAPI request machinery, and the two call sites can never drift.
"""
from __future__ import annotations

import base64
import logging
import os
import pathlib
import re
import tempfile
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select

from .matcher import match_billing_workbook, BillingMatch
from . import invoice as invoice_mod
from . import summary as summary_mod

logger = logging.getLogger(__name__)

AO_FROM = os.getenv("MAIL_FROM_AO", os.getenv("MAIL_FROM",
                    "Array Operator <reports@arrayoperator.com>"))


# ─── scheduling helpers ─────────────────────────────────────────────────────

def next_send_at(cadence: str, after: Optional[datetime] = None) -> datetime:
    """The next delivery instant (09:00 UTC, 1st of the next month/quarter)."""
    after = after or datetime.utcnow()
    year, month = after.year, after.month
    if cadence == "quarterly":
        # next quarter-start month among 1,4,7,10
        for m in (1, 4, 7, 10, 13):
            ny, nm = (year, m) if m <= 12 else (year + 1, 1)
            cand = datetime(ny, nm, 1, 9, 0)
            if cand > after:
                return cand
        return datetime(year + 1, 1, 1, 9, 0)
    # monthly (default): the 1st of next month
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return datetime(ny, nm, 1, 9, 0)


# ─── quarter labels ──────────────────────────────────────────────────────────

_QUARTER_LABEL_RE = re.compile(r"^(\d{4})-Q([1-4])$")


def _is_quarter_label(label) -> bool:
    """True for a 'YYYY-Qn' quarter label (vs the 'YYYY-MM' month labels)."""
    return bool(label) and bool(_QUARTER_LABEL_RE.match(str(label)))


def _quarter_of_month_label(label: str) -> tuple[int, int]:
    """'2026-05' -> (2026, 2)."""
    y, m = str(label).split("-")
    return int(y), (int(m) - 1) // 3 + 1


def _quarter_month_labels(year: int, quarter: int) -> list[str]:
    """(2026, 2) -> ['2026-04', '2026-05', '2026-06']."""
    first = 3 * (quarter - 1) + 1
    return [f"{year:04d}-{m:02d}" for m in (first, first + 1, first + 2)]


def _month_label_pretty(label: str) -> str:
    """'2026-04' -> 'April 2026' (falls back to the raw label)."""
    try:
        y, m = str(label).split("-")
        return date(int(y), int(m), 1).strftime("%B %Y")
    except (ValueError, TypeError):
        return str(label)


# ─── attachment generation ──────────────────────────────────────────────────

def build_match(sub, period_label: Optional[str] = None) -> BillingMatch:
    """Rebuild a BillingMatch from a subscription.

    period_label ("YYYY-MM") targets a SPECIFIC historical period for the manual /
    utility-bill path (default = the latest billed period). Used to backfill an
    offtaker's generation spreadsheet with each missing month's real, canonically
    computed figures.

    Two paths:
      * workbook-driven (the original) — re-parse the stored .xlsx bytes.
      * manual (no workbook) — the operator typed the customer in; synthesize a
        BillingMatch from the typed allocation_pct × the array's period
        generation. Both produce the same BillingMatch shape so every downstream
        consumer (invoice/summary renderers, delivery, drafts) is unchanged.
    """
    # Bill from the GMP bill (percent-of-array: allocation_pct × the bill's generation)
    # whenever the operator has EXPLICITLY configured it — both a linked utility account
    # AND a share are set. That explicit config OVERRIDES a stored workbook (which would
    # otherwise re-parse the uploaded sheet), so a spreadsheet offtaker can be switched to
    # bill-driven billing just by setting bill + share. A budget bill still overrides the
    # final total below, unchanged. Pure-workbook offtakers (no bill/share set) are
    # unaffected; pure percent-of-array offtakers (no workbook) already take this path.
    explicit_pct = (
        getattr(sub, "utility_account_id", None) is not None
        and getattr(sub, "allocation_pct", None) is not None
    )
    if (not sub.source_workbook) or explicit_pct:
        m = build_manual_match(sub, period_label=period_label)
    else:
        m = match_billing_workbook(bytes(sub.source_workbook), allow_llm=False)
    # Sequential invoice numbering: when the operator set a starting number, the
    # running counter (invoice_number_next) replaces the default period-date number
    # on the rendered invoice. The counter is advanced on a real send (deliver).
    nxt = getattr(sub, "invoice_number_next", None)
    if nxt is not None and m is not None and getattr(m, "computed_invoice", None):
        m.computed_invoice["invoice_number"] = str(nxt)
    # Budget billing: a per-offtaker fixed final amount the operator entered overrides
    # the calculated Amount Due. All line items still compute/show; only the total is
    # this number. (Flagged so the invoice + email can note it's a budget bill.)
    budget = getattr(sub, "budget_amount_usd", None)
    if budget is not None and m is not None and getattr(m, "computed_invoice", None):
        ci = m.computed_invoice
        # Preserve the CALCULATED solar credit value before the fixed budget total
        # overrides the amount owed, so the email + invoice can show BOTH as distinct
        # rows: what the solar credit was worth vs. the budgeted amount actually billed.
        ci.setdefault("solar_credit_value", ci.get("amount_owed"))
        ci["amount_owed"] = float(budget)
        ci["budget_override"] = True
    return m


# Default Vermont solar value used to price a manual customer's share when no
# workbook AND no explicit rate supplies a tariff. Mirrors reconciliation's
# VT_DEFAULT_RATE. Kept as the final fallback below the per-customer rate and
# the operator's global default rate.
MANUAL_TARIFF = 0.18398
MANUAL_BILLING_RATE = 0.9

# Default discount when neither the customer nor the operator set one: 10% off
# the net rate (i.e. the customer pays 90% — matches the legacy MANUAL_BILLING_RATE).
DEFAULT_DISCOUNT = 0.10


def resolve_discount_pricing(sub, *, period_end=None, region=None,
                             first_connect_date=None, ctx=None) -> dict:
    """Resolve the discount-model pricing for a customer.

    invoice = produced kWh × net_rate × (1 − discount).

    Precedence per field (customer override → operator global → AUTO schedule →
    legacy flat → built-in default):
      net_rate : sub.net_rate_per_kwh → tenant.default_net_rate_per_kwh
                 → auto RateSchedule (blended, by utility/location/age/month)
                 → legacy flat rate → MANUAL_TARIFF (VT reference, last resort)
      For offtakers bound to a utility bill, build_manual_match OVERRIDES this
      chain: customer → master global → THIS offtaker's bound bill credit rate
      (per sub-account, never a fleet median).
      discount : sub.discount_pct → tenant.default_discount_pct → DEFAULT_DISCOUNT (0.10)

    period_end/region/first_connect_date feed the auto schedule lookup; when not
    passed they're best-effort derived from the sub's array.

    `ctx` (see build_pricing_ctx) batches the per-sub DB work for LIST callers:
    without it each call opens its own session + 3 queries, which turned the
    800-offtaker subscriptions list into a 17-second N+1 (caught at Anna scale).
    Semantics are identical with or without ctx.

    Returns {net_rate, discount_pct, effective_rate, net_source, discount_source,
    net_rate_note} where effective_rate = net_rate × (1 − discount).
    """
    from ..db import SessionLocal
    from ..models import Tenant, Array, UtilityAccount
    from ..rate_schedule import resolve_net_rate

    sub_net = getattr(sub, "net_rate_per_kwh", None)
    sub_disc = getattr(sub, "discount_pct", None)
    sub_flat = getattr(sub, "rate_per_kwh", None)   # legacy flat $/kWh override

    g_net = g_disc = g_flat = None
    net_note = None

    def _resolve_with(db, tenant, array_fields, rate_memo):
        nonlocal g_net, g_disc, g_flat, net_note
        if tenant:
            g_net = getattr(tenant, "default_net_rate_per_kwh", None)
            g_disc = getattr(tenant, "default_discount_pct", None)
            g_flat = getattr(tenant, "default_billing_rate_per_kwh", None)

        # Best-effort derive the array's utility/region/age for the auto lookup
        # when the caller didn't supply them.
        _region, _fc, _provider = region, first_connect_date, None
        arr_id = getattr(sub, "array_id", None)
        if arr_id is not None:
            if array_fields is not None:
                af = array_fields.get(arr_id)
                if af:
                    if _region is None:
                        _region = af[0]
                    if _fc is None:
                        _fc = af[1]
                    _provider = af[2]
            else:
                arr = db.get(Array, arr_id)
                if arr is not None:
                    if _region is None:
                        _region = arr.region
                    if _fc is None:
                        _fc = arr.first_connect_date
                    acct = db.execute(
                        select(UtilityAccount).where(UtilityAccount.array_id == arr_id)
                    ).scalars().first()
                    if acct is not None:
                        _provider = acct.provider

        # ── net rate ──  (customer → master global → AUTO schedule → legacy flat →
        # VT reference). Bill-bound offtakers re-resolve in build_manual_match so
        # blank master falls through to THIS offtaker's sub-account bill rate.
        if sub_net is not None and sub_net > 0:
            return float(sub_net), "customer"
        if g_net is not None and g_net > 0:
            return float(g_net), "global"
        memo_key = (_provider, _region, _fc, period_end)
        auto = rate_memo.get(memo_key) if rate_memo is not None else None
        if auto is None:
            auto = resolve_net_rate(db, provider=_provider, region=_region,
                                    first_connect_date=_fc, period_end=period_end)
            if rate_memo is not None:
                rate_memo[memo_key] = auto
        if auto.source in ("schedule", "schedule_provisional"):
            net_note = auto.note
            return auto.rate, "auto_" + auto.source
        if sub_flat is not None and sub_flat > 0:
            return float(sub_flat), "legacy_flat_customer"
        if g_flat is not None and g_flat > 0:
            return float(g_flat), "legacy_flat_global"
        # auto returned the VT default — use it and keep its provenance.
        net_note = auto.note
        return auto.rate, "vt_default"

    if ctx is not None:
        net_rate, net_source = _resolve_with(
            ctx["db"], ctx.get("tenant"), ctx.get("array_fields"),
            ctx.get("rate_memo"))
    else:
        with SessionLocal() as db:
            net_rate, net_source = _resolve_with(
                db, db.get(Tenant, sub.tenant_id), None, None)

    # ── discount ──
    if sub_disc is not None and 0 <= sub_disc < 1:
        discount, discount_source = float(sub_disc), "customer"
    elif g_disc is not None and 0 <= g_disc < 1:
        discount, discount_source = float(g_disc), "global"
    elif net_source in ("legacy_flat_customer", "legacy_flat_global"):
        # A legacy flat rate already encodes the price; don't re-discount it.
        discount, discount_source = 0.0, "legacy_flat"
    else:
        discount, discount_source = DEFAULT_DISCOUNT, "default"

    effective_rate = round(net_rate * (1 - discount), 6)
    return {
        "net_rate": net_rate,
        "discount_pct": discount,
        "effective_rate": effective_rate,
        "net_source": net_source,
        "discount_source": discount_source,
        "net_rate_note": net_note,
        # Tenant master override (None when blank). Bill-bound pricing uses this
        # so a filled master rate wins over each offtaker's sub-account bill rate,
        # while a blank master leaves each offtaker on their own bill's credit rate.
        "tenant_net_rate": (float(g_net) if g_net is not None and g_net > 0 else None),
    }


def build_pricing_ctx(db, tenant) -> dict:
    """Batched context for resolve_discount_pricing over a LIST of subs: the
    tenant's pricing defaults, every array's (region, first_connect_date,
    provider) via two bulk queries, and a memo for the schedule lookups —
    replaces a per-sub session + 3 queries (the 17s N+1 the 800-offtaker list
    exposed). Build it inside an open session and use it before that session
    closes (helpers never outlive their session — the reconcile pool-leak
    lesson)."""
    from ..models import Array, UtilityAccount
    provider_by_array: dict[int, str] = {}
    for aid, prov in db.execute(
            select(UtilityAccount.array_id, UtilityAccount.provider)
            .where(UtilityAccount.tenant_id == tenant.id,
                   UtilityAccount.array_id.isnot(None))
            .order_by(UtilityAccount.id)):
        provider_by_array.setdefault(aid, prov)
    fields: dict[int, tuple] = {}
    for arr_id, reg, fc in db.execute(
            select(Array.id, Array.region, Array.first_connect_date)
            .where(Array.tenant_id == tenant.id)):
        fields[arr_id] = (reg, fc, provider_by_array.get(arr_id))
    return {"db": db, "tenant": tenant, "array_fields": fields, "rate_memo": {}}


def resolve_rate_per_kwh(sub) -> tuple[Optional[float], str]:
    """DEPRECATED — superseded by resolve_discount_pricing(). Kept for any
    external caller; returns the effective $/kWh under the discount model."""
    p = resolve_discount_pricing(sub)
    src = "customer" if p["discount_source"] == "customer" or p["net_source"] == "customer" else \
          ("global" if "global" in (p["net_source"], p["discount_source"]) else "vt_default")
    return p["effective_rate"], src


def _array_period_kwh(db, array_id: int) -> tuple[Optional[float], Optional[date], Optional[date], Optional[str], Optional[str]]:
    """The array's most recent full-month generation: (array_kwh, start, end,
    month_label, dom_source). Prefers DailyGeneration; falls back to Bill.kwh_generated.
    dom_source ∈ 'daily_csv' (metered/uploaded) | 'bill_prorate' (estimate-dominated
    month) | 'utility_bill' (a real bill) | None. Returns (None,)*5 when no data yet."""
    from ..models import DailyGeneration, Bill, UtilityAccount

    # Prefer DailyGeneration: pick the latest (year, month) that has rows. Track the
    # SOURCE so provenance is honest (audit #8): a month dominated by 'bill_prorate'
    # (a flat-smeared utility bill) is an ESTIMATE, not metered/uploaded data.
    rows = db.execute(
        select(DailyGeneration.day, DailyGeneration.kwh, DailyGeneration.source)
        .where(DailyGeneration.array_id == array_id)
        .order_by(DailyGeneration.day.desc())
    ).all()
    if rows:
        latest_day = rows[0].day
        y, m = latest_day.year, latest_day.month
        month_rows = [r for r in rows if r.day.year == y and r.day.month == m]
        total = sum(float(r.kwh or 0) for r in month_rows)
        days = sorted(r.day for r in month_rows)
        label = latest_day.strftime("%Y-%m")
        prorate = sum(float(r.kwh or 0) for r in month_rows
                      if (r.source or "") == "bill_prorate")
        dom = "bill_prorate" if (total > 0 and prorate >= 0.5 * total) else "daily_csv"
        return round(total, 1), days[0], days[-1], label, dom

    # Fallback: the array's bills (kwh_generated) for the most recent period — a real
    # utility-bill figure (provenance 'utility_bill', not the prorated daily estimate).
    bill = db.execute(
        select(Bill)
        .join(UtilityAccount, Bill.account_id == UtilityAccount.id)
        .where(UtilityAccount.array_id == array_id,
               Bill.kwh_generated.isnot(None),
               Bill.period_end.isnot(None))
        .order_by(Bill.period_end.desc())
    ).scalars().first()
    if bill is not None:
        ps = bill.period_start.date() if bill.period_start else None
        pe = bill.period_end.date() if bill.period_end else None
        label = pe.strftime("%Y-%m") if pe else None
        return round(float(bill.kwh_generated), 1), ps, pe, label, "utility_bill"
    return None, None, None, None, None


def _array_period_kwh_sourced(
    db, array_id: int
) -> tuple[Optional[float], Optional[date], Optional[date], Optional[str], Optional[str]]:
    """Source-agnostic period generation for an array, with provenance.

    Precedence (per Ford's call + the GMP_DAILY_READ_CONTRACT):
      1. GMP daily-read contract (api/reports/gmp_daily_read) — the authoritative
         metered source. We call its functions only; we never touch the gmp_*
         tables/ORM directly (storage stays the data-sponge agent's).
      2. DailyGeneration / Bill (the legacy path) when GMP has no coverage yet
         (e.g. backfill not run / GMP auth blocked).

    Returns (kwh, start, end, label, kwh_source) where kwh_source is one of
    'gmp_api' | 'daily_csv' | None. Mirrors _array_period_kwh's "latest month
    present" semantics so downstream math is unchanged regardless of source.
    """
    # 1) Try the GMP contract first. Defensive: a provisional module / empty
    #    tables must degrade to the fallback, never raise into invoice math.
    try:
        from ..reports import gmp_daily_read as gdr
        months = gdr.get_monthly_totals(array_id, db=db)
        if months:
            latest = months[-1]                       # ascending → last = newest
            y, m = latest["year"], latest["month"]
            series = [r for r in gdr.get_daily_series(array_id, db=db)
                      if r["day"].year == y and r["day"].month == m]
            if series:
                days = sorted(r["day"] for r in series)
                return (round(float(latest["kwh"]), 1), days[0], days[-1],
                        f"{y:04d}-{m:02d}", "gmp_api")
    except Exception:  # noqa: BLE001 — provisional contract / missing tables
        logger.warning("GMP daily-read unavailable for array %s; falling back",
                       array_id, exc_info=True)

    # 2) Legacy fallback: DailyGeneration → Bill, with honest provenance from
    #    _array_period_kwh (daily_csv = metered/uploaded, bill_prorate = estimate-
    #    dominated month, utility_bill = a real bill) instead of a blanket 'daily_csv'.
    kwh, start, end, label, dom = _array_period_kwh(db, array_id)
    return kwh, start, end, label, (dom if kwh is not None else None)


# Honest operator-set rate sources for a VEC/SmartHub offtaker. A VEC invoice may
# ONLY use a rate the operator actually entered (per-offtaker or operator-global,
# incl. the legacy flat overrides) — NEVER the auto schedule's VT/GMP default,
# because VEC bills don't publish a net-metering credit rate and inventing one
# would mis-bill a real customer (Ford's hard no, "model A").
_SMARTHUB_HONEST_RATE_SOURCES = frozenset(
    {"customer", "global", "legacy_flat_customer", "legacy_flat_global"}
)


def _build_smarthub_offtaker_match(sub, operator, warnings) -> BillingMatch:
    """VEC/SmartHub offtaker: price the offtaker's allocation_pct of the array's
    MEASURED generation at an OPERATOR-ENTERED net rate. SmartHub bills carry no
    excess+credit, so we REQUIRE an operator rate (per-offtaker or operator-global)
    and NEVER use the GMP/VT default. Skips (waits) until both the generation and a
    real operator rate are present."""
    from ..db import SessionLocal
    from ..models import UtilityAccount
    from .matcher import Period, compute_invoice

    # Re-fetch the bound account in a fresh session (we only have its id on `sub`).
    with SessionLocal() as db:
        acct = db.get(UtilityAccount, sub.utility_account_id)
        prov = (acct.provider or "").lower() if acct else "vec"
        PROV = prov.upper()
        array_name = (acct.nickname if acct and acct.nickname
                      else (f"{PROV} acct {acct.account_number}" if acct else None))
        arr_id = acct.array_id if acct else None

        # MEASURED generation for the array (DailyGeneration / GMP-read contract /
        # Bill fallback — honest provenance in gen_src). VEC daily generation is
        # populated from the SmartHub net-export pull (api/adapters/smarthub).
        if arr_id is not None:
            gen_kwh, start, end, label, gen_src = _array_period_kwh_sourced(db, arr_id)
        else:
            gen_kwh = None
            start = end = None
            label = None
            gen_src = None
            warnings.append(
                f"This {PROV} account isn't linked to an array yet — link it to "
                f"the array to bill its generation.")

    # Rate: REQUIRE an operator-entered rate. Never the auto/VT default.
    pricing = resolve_discount_pricing(sub, period_end=end)
    rate_ok = pricing["net_source"] in _SMARTHUB_HONEST_RATE_SOURCES
    if not rate_ok:
        warnings.append(
            f"Set a net-metering credit rate for this {PROV} offtaker — {PROV} "
            f"bills don't publish one, so the invoice waits for the rate you enter "
            f"(we never bill {PROV} at the GMP/VT default).")

    # Use the GROUP share (array_share_pct) for a sub-metered offtaker, mirroring the GMP
    # real_math path. allocation_pct is pinned to 1.0 there (100% of their OWN sub-meter),
    # and this VEC path multiplies the ARRAY's measured generation — so allocation_pct=1.0
    # would bill them the WHOLE array (an over-count / the "double math" class Ford flagged
    # 2026-07-10). array_share_pct is None for a plain percent-of-array VEC offtaker, so
    # their allocation_pct share is used unchanged. (No live VEC offtaker is in the
    # sub-metered state today — all 902 array_share_pct offtakers are GMP — so this is
    # preventive; it changes no current invoice.)
    _grp_share = getattr(sub, "array_share_pct", None)
    pct = _grp_share if _grp_share is not None else (sub.allocation_pct or 0.0)
    if not pct:
        warnings.append("No allocation % set for this offtaker.")

    # Billable only when BOTH measured generation AND a real operator rate exist.
    billable = (gen_kwh is not None) and rate_ok
    array_kwh = gen_kwh if billable else None
    net_rate = pricing["net_rate"] if rate_ok else 0.0
    discount = pricing["discount_pct"]
    billing_rate = 1.0 - discount

    customer_kwh = round((array_kwh or 0.0) * pct, 2)
    computed = compute_invoice(customer_kwh, net_rate, 0.0,
                               billing_rate, "percent_of_array", None)
    computed["invoice_number"] = end.strftime("%Y-%m") if end else label
    computed["period_start"] = start.isoformat() if start else None
    computed["period_end"] = end.isoformat() if end else None
    computed["month"] = label
    computed["project_total_kwh"] = (array_kwh or 0.0)
    computed["array_kwh"] = (array_kwh or 0.0)
    # For VEC this is MEASURED generation (no excess+credit on the bill); kept on
    # the excess_kwh key purely for downstream-shape parity with the GMP path.
    computed["excess_kwh"] = (array_kwh or 0.0)
    computed["solar_credit_usd"] = None
    computed["net_rate_per_kwh"] = round(net_rate, 6)
    computed["discount_pct"] = round(discount, 6)
    computed["effective_rate_per_kwh"] = round(net_rate * billing_rate, 6)
    computed["net_rate_source"] = pricing["net_source"] if rate_ok else "needs_rate"
    computed["net_rate_note"] = (
        "the net-metering credit rate you entered (" + PROV
        + " bills don't publish one)") if rate_ok else None
    computed["discount_source"] = pricing["discount_source"]
    computed["rate_per_kwh"] = round(net_rate * billing_rate, 6)
    computed["rate_source"] = pricing["discount_source"]
    # HONEST provenance — measured generation, NOT a utility bill.
    computed["kwh_source"] = gen_src or "smarthub_generation"
    # The send-guard flag: only True when this is a real, billable invoice.
    computed["has_utility_bill"] = billable

    period = Period(
        month=label, start=start, end=end,
        array_kwh=(array_kwh or 0.0), customer_kwh=customer_kwh,
        tariff=net_rate, adder=0.0,
    )
    return BillingMatch(
        matched=True, confidence=1.0, source="manual", data_sheet=None,
        customer={"name": sub.customer_name, "email": sub.client_email},
        allocation_pct=pct,
        billing_rate=billing_rate, billing_model="percent_of_array",
        periods=[period], latest_period=period,
        template={"title": "Invoice - Solar Power Generation", "operator": operator},
        computed_invoice=computed,
        project_totals={
            "total_array_kwh": (array_kwh or 0.0),
            "total_customer_kwh": customer_kwh,
            "array_name": array_name,
        },
        warnings=warnings,
    )


def _utility_bill_period_kwh(
    db, utility_account_id: int
) -> tuple[Optional[float], Optional[date], Optional[date], Optional[str]]:
    """OFFTAKER source of truth: the utility's PAPER BILL generation for ONE GMP
    account's most recent billing period — and NOTHING else.

    Reads Bill.kwh_generated for the bound utility_account_id (the paper copy's
    stated generation per billing period). This is deliberately the ONLY source
    for offtaker invoices: NO vendor/inverter telemetry, NO GMP hourly-interval
    data, NO DailyGeneration CSV. If no utility bill covers a period yet, returns
    (None, None, None, None) so the caller SKIPS/waits — it must never fabricate
    or substitute another source.

    Returns (kwh, period_start, period_end, label). label = period_end's YYYY-MM.
    """
    from ..models import Bill

    bill = db.execute(
        select(Bill)
        .where(Bill.account_id == utility_account_id,
               Bill.kwh_generated.isnot(None),
               Bill.period_end.isnot(None))
        .order_by(Bill.period_end.desc())
    ).scalars().first()
    if bill is None:
        return None, None, None, None
    ps = bill.period_start.date() if bill.period_start else None
    pe = bill.period_end.date() if bill.period_end else None
    label = pe.strftime("%Y-%m") if pe else None
    return round(float(bill.kwh_generated), 1), ps, pe, label


def _utility_bill_credit(
    db, utility_account_id: int, target_label: Optional[str] = None
) -> tuple[Optional[float], Optional[float], Optional[float],
           Optional[date], Optional[date], Optional[str], Optional[str]]:
    """OFFTAKER billing basis (Ford/Bruce's model, option B): the latest period's
    EXCESS sent to grid valued at the net-metering credit rate.

    Delegates to rate_schedule.resolve_offtaker_excess_credit: a CASHED month uses
    the bill's own EXCESS+SOLCRED rate; a BANKED month falls back to a REFERENCE
    rate (the account's own cashing history → the fleet median for the array's age
    → DEFAULT_CREDIT_RATE) so the offtaker still pays for the solar they received
    while the host keeps the banked credit (trued up annually). Never gross kWh × a
    flat rate, and never the $10,659-for-$2 over-charge.

    Returns (excess_kwh, credit_usd, credit_rate, period_start, period_end, label,
    rate_source) — rate_source ∈ {'bill_cash','reference'} — or (None, …) when the
    latest bill has no excess to bill (→ caller SKIPS).
    """
    from ..rate_schedule import resolve_offtaker_excess_credit
    r = resolve_offtaker_excess_credit(db, utility_account_id, target_label)
    if r is None:
        return None, None, None, None, None, None, None
    return r


def settled_periods_for_sub(sub) -> list[dict]:
    """The billable periods an operator can DRAFT for this offtaker (Bruce
    2026-07-07, Comment 4): every settled utility bill with billable excess on
    the offtaker's bound account, newest first, each as a pickable option.

    Returns [{label, pretty, is_latest}] where `label` is the exact string
    build_match(sub, period_label=label) accepts:
      • monthly cadence → 'YYYY-MM' month labels;
      • quarterly cadence → 'YYYY-Qn' quarter labels (deduped from the covered
        months), so the selector mirrors how the invoice is actually built.
    Empty list when the offtaker isn't utility-bill bound or nothing is settled
    yet (the UI then keeps the implicit 'latest bill' default). Owns its own
    session — safe to call from any request/caller lifecycle."""
    from ..db import SessionLocal
    from ..models import Bill

    uaid = getattr(sub, "utility_account_id", None)
    if uaid is None:
        return []
    quarterly = (getattr(sub, "cadence", None) or "monthly") == "quarterly"
    with SessionLocal() as db:
        bills = db.execute(
            select(Bill)
            .where(Bill.account_id == uaid, Bill.period_end.isnot(None))
            .order_by(Bill.period_end.desc())
        ).scalars().all()
    # A billable month = a settled bill that sent real excess to the grid. (A
    # zero-excess/banked month can't be invoiced on its own, so it isn't offered.)
    month_labels: list[str] = []
    seen: set[str] = set()
    for b in bills:
        pe = b.period_end.date() if isinstance(b.period_end, datetime) else b.period_end
        if pe is None:
            continue
        if b.kwh_sent_to_grid is None or float(b.kwh_sent_to_grid) <= 0:
            continue
        lbl = pe.strftime("%Y-%m")
        if lbl in seen:
            continue
        seen.add(lbl)
        month_labels.append(lbl)          # already newest-first (query DESC)
    if not month_labels:
        return []
    if quarterly:
        q_seen: set[str] = set()
        out: list[dict] = []
        for ml in month_labels:           # newest-first → newest quarter first
            y, q = _quarter_of_month_label(ml)
            qlbl = f"{y:04d}-Q{q}"
            if qlbl in q_seen:
                continue
            q_seen.add(qlbl)
            out.append({"label": qlbl, "pretty": f"Q{q} {y}"})
    else:
        out = [{"label": ml, "pretty": _month_label_pretty(ml)} for ml in month_labels]
    if out:
        out[0]["is_latest"] = True        # the implicit default
    return out


def _utility_bill_credit_quarter(db, utility_account_id: int,
                                 target_label: Optional[str] = None) -> Optional[dict]:
    """QUARTERLY offtaker billing basis: sum the FULL quarter's settled utility
    bills — every month's EXCESS sent to grid + its own net-metering credit —
    into one aggregate (Ford, backlog #6: a quarterly cadence must never bill
    just one of the three months).

    Anchoring mirrors the monthly path: without `target_label` we bill the
    calendar quarter of the account's NEWEST bill with billable excess; a
    'YYYY-Qn' target pins a specific historical quarter.

    HONESTY RULES (same paper-bill-only discipline as the monthly path):
      • only settled utility bills count — a month with no settled bill is
        MISSING, never estimated, prorated, or silently dropped;
      • a quarter with a missing month is returned complete=False so the caller
        HOLDS the invoice (delivery waits for the bill) instead of under-billing;
      • the ONE exception: months before the account's FIRST-EVER settled bill
        (service started mid-quarter) aren't "missing" — the quarter bills the
        covered months with the covered range clearly marked (partial_start);
      • a settled month with zero excess contributes 0 kWh (it is covered).

    Returns None when the account has no bill with billable excess at all
    (→ caller waits, exactly like the monthly path). Otherwise a dict:
      {label:'YYYY-Qn', complete:bool, partial_start:bool,
       covered_labels:[...], missing_labels:[...],
       months:[{label, excess_kwh, credit_usd, credit_rate, rate_source}],
       excess_kwh, credit_usd, credit_rate (blended = credit/excess),
       start, end, rate_source ('bill_cash' | 'reference' when ANY month
       needed a reference rate)}.
    """
    from ..models import Bill
    from ..rate_schedule import resolve_offtaker_excess_credit

    bills = db.execute(
        select(Bill)
        .where(Bill.account_id == utility_account_id,
               Bill.period_end.isnot(None))
        .order_by(Bill.period_end.asc())
    ).scalars().all()

    def _lbl(b) -> Optional[str]:
        pe = b.period_end.date() if isinstance(b.period_end, datetime) else b.period_end
        return pe.strftime("%Y-%m") if pe else None

    # A SETTLED bill states real figures (generation and/or excess). Stub rows
    # with neither are ignored — they don't cover a month.
    settled: dict[str, list] = {}
    excess_labels: list[str] = []
    for b in bills:
        if b.kwh_generated is None and b.kwh_sent_to_grid is None:
            continue
        lb = _lbl(b)
        if not lb:
            continue
        settled.setdefault(lb, []).append(b)
        if b.kwh_sent_to_grid is not None and float(b.kwh_sent_to_grid) > 0:
            excess_labels.append(lb)
    if not settled or not excess_labels:
        return None                      # nothing billable yet → caller waits

    if target_label and _is_quarter_label(target_label):
        y, q = _QUARTER_LABEL_RE.match(str(target_label)).groups()
        year, quarter = int(y), int(q)
    else:
        year, quarter = _quarter_of_month_label(max(excess_labels))
    q_label = f"{year:04d}-Q{quarter}"
    months = _quarter_month_labels(year, quarter)

    first_ever = min(settled)
    required = [m for m in months if m >= first_ever]
    if not required:
        return None                      # quarter predates the account entirely
    covered = [m for m in required if m in settled]
    missing = [m for m in required if m not in settled]
    partial_start = any(m < first_ever for m in months)

    month_rows: list[dict] = []
    excess_total = 0.0
    credit_total = 0.0
    any_reference = False
    starts: list[date] = []
    ends: list[date] = []
    for m in covered:
        r = resolve_offtaker_excess_credit(db, utility_account_id, m)
        if r is not None:
            m_excess, m_credit, m_rate, m_ps, m_pe, _, m_src = r
            excess_total += float(m_excess or 0.0)
            credit_total += float(m_credit or 0.0)
            any_reference = any_reference or (m_src == "reference")
            month_rows.append({"label": m, "excess_kwh": m_excess,
                               "credit_usd": m_credit, "credit_rate": m_rate,
                               "rate_source": m_src})
            if m_ps:
                starts.append(m_ps)
            if m_pe:
                ends.append(m_pe)
        else:
            # Settled month with no billable excess → covered, contributes 0.
            month_rows.append({"label": m, "excess_kwh": 0.0, "credit_usd": 0.0,
                               "credit_rate": None, "rate_source": None})
            for b in settled[m]:
                ps = b.period_start.date() if isinstance(b.period_start, datetime) else b.period_start
                pe = b.period_end.date() if isinstance(b.period_end, datetime) else b.period_end
                if ps:
                    starts.append(ps)
                if pe:
                    ends.append(pe)

    excess_total = round(excess_total, 1)
    credit_total = round(credit_total, 2)
    blended = round(credit_total / excess_total, 6) if excess_total > 0 else None
    return {
        "label": q_label,
        "complete": not missing,
        "partial_start": partial_start,
        "covered_labels": covered,
        "missing_labels": missing,
        "months": month_rows,
        "excess_kwh": excess_total,
        "credit_usd": credit_total,
        "credit_rate": blended,
        "start": min(starts) if starts else None,
        "end": max(ends) if ends else None,
        "rate_source": "reference" if any_reference else "bill_cash",
    }


def _normalized_allocations(sub) -> list[dict]:
    """Return a clean list of {array_id:int, allocation_pct:float} for a sub.

    Reads sub.array_allocations (the multi-array field). Coerces types, drops
    rows with no array_id or a non-positive pct. Returns [] when the field is
    absent/empty — callers then use the legacy single array_id path. Never raises.
    """
    raw = getattr(sub, "array_allocations", None)
    if not raw:
        return []
    out: list[dict] = []
    try:
        for r in raw:
            try:
                aid = int(r.get("array_id"))
            except (TypeError, ValueError, AttributeError):
                continue
            try:
                pct = float(r.get("allocation_pct"))
            except (TypeError, ValueError, AttributeError):
                continue
            if pct <= 0:
                continue
            out.append({"array_id": aid, "allocation_pct": pct})
    except TypeError:
        return []
    return out


def _array_group_excess_for_sub(sub, period_label=None, period_labels=None):
    """The array's GROUP excess (its host bill's kwh_sent_to_grid) — the pool GMP
    allocates among the array's offtakers by share — for the "real math" invoice.
    Returns None (→ fall back to GMP's own-bill figure) when it can't be trusted:
    a multi-array offtaker, no linked array/host bill, or the host account IS the
    offtaker's own account (single-meter — then share × host would double-count).
    Never raises — any error falls back to GMP's own-bill figure (the safe default
    for a billing path).

    `period_labels` (quarterly cadence) sums the host bills across ALL the given
    'YYYY-MM' months — and requires a host bill for EVERY one of them, else None
    (a partially-covered quarter must fall back to gmp_credited, never mix bases
    month-by-month or silently under-count the pool).

    Opens its OWN short-lived session. build_manual_match calls this AFTER its
    `with SessionLocal()` block has already closed, so a passed-in session was
    dead — `.execute()` on it autobegins a fresh transaction that checks out a
    pool connection which is never returned (the `with` won't fire again). One
    leak per offtaker with a share set exhausted the pool: reconcile-bills /
    audit-by-array HUNG then 500'd (`QueuePool ... timed out`) on a 60-offtaker
    tenant. Owning the session here makes the helper leak-proof regardless of
    caller lifecycle."""
    from ..db import SessionLocal
    try:
        with SessionLocal() as db:
            return _array_group_excess_for_sub_inner(db, sub, period_label,
                                                     period_labels)
    except Exception:
        return None


def _array_group_excess_for_sub_inner(db, sub, period_label=None,
                                      period_labels=None):
    from ..models import UtilityAccount, Bill
    aid = getattr(sub, "array_id", None)
    if aid is None or getattr(sub, "array_allocations", None):
        return None
    acct_id = db.execute(
        select(UtilityAccount.id).where(UtilityAccount.array_id == aid)
        .order_by(UtilityAccount.id)
    ).scalars().first()
    if acct_id is None or acct_id == getattr(sub, "utility_account_id", None):
        return None   # no host account, or it's the offtaker's own meter
    bills = list(db.execute(
        select(Bill).where(Bill.account_id == acct_id, Bill.period_end.isnot(None))
        .order_by(Bill.period_end.desc())
    ).scalars().all())
    if not bills:
        return None

    def _bill_excess(b):
        v = getattr(b, "kwh_sent_to_grid", None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
        return float(b.kwh_generated) if b.kwh_generated is not None else None

    def _bill_label(b):
        pe = b.period_end.date() if hasattr(b.period_end, "date") else b.period_end
        return pe.strftime("%Y-%m") if pe else None

    if period_labels:
        # Quarterly: one host bill per month, ALL months required.
        by_label: dict[str, float] = {}
        for b in bills:
            lb = _bill_label(b)
            if lb in period_labels and lb not in by_label:
                v = _bill_excess(b)
                if v is not None:
                    by_label[lb] = v
        if any(lb not in by_label for lb in period_labels):
            return None
        return round(sum(by_label[lb] for lb in period_labels), 1)

    bill = None
    if period_label:
        for b in bills:
            if _bill_label(b) == period_label:
                bill = b
                break
    bill = bill or bills[0]
    return _bill_excess(bill)


def _operator_company_name(tenant_id):
    """The operator's own company name (what they filled out at signup) to print on
    offtaker invoices instead of the generic 'Your solar array owner'. Falls back
    company_name -> operator_name -> name; None only if the tenant truly set none."""
    if not tenant_id:
        return None
    from ..db import SessionLocal
    from ..models import Tenant
    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
    if not t:
        return None
    return (getattr(t, "company_name", None) or getattr(t, "operator_name", None)
            or getattr(t, "name", None))


def _platform_from_email(product) -> str:
    """Just the address part of the platform From, so the operator's display name
    can ride on the platform's verified sending domain when they haven't set up a
    sending domain of their own."""
    import re
    from .. import branding
    fa = branding.from_address(product)
    m = re.search(r"<([^>]+)>", fa)
    return m.group(1) if m else fa


def build_manual_match(sub, period_label: Optional[str] = None) -> BillingMatch:
    """Synthesize a BillingMatch for a manually-entered customer (no workbook).

    The customer's share = allocation_pct × the array's most recent period
    generation, priced at the default VT solar value. Always returns a matched
    BillingMatch (with a zero-kWh period when the array has no data yet) so the
    demo can render and send; warnings flag any thin data.
    """
    from ..db import SessionLocal
    from ..models import Array
    from .matcher import Period, compute_invoice

    operator = _operator_company_name(getattr(sub, "tenant_id", None))
    pct = sub.allocation_pct
    warnings: list[str] = []
    array_kwh: Optional[float] = None
    start = end = None
    label: Optional[str] = None
    array_name: Optional[str] = None
    kwh_source: Optional[str] = None

    # ── OFFTAKER ↔ UTILITY BILL path (Ford's rule) ────────────────────────────
    # When the offtaker is bound to a GMP utility account, their invoice is
    # computed EXCLUSIVELY from that account's utility PAPER BILLS — the metered
    # generation the utility itself states per billing period. NO vendor/inverter
    # data, NO GMP hourly-interval data, NO daily CSV, and NO fallback to any of
    # those. If no utility bill covers a period yet, kWh stays None and delivery
    # SKIPS (waits on the utility bill) — it is never fabricated or substituted.
    if getattr(sub, "utility_account_id", None) is not None:
        from ..models import UtilityAccount
        from ..adapters import is_smarthub_provider
        # A VEC/SmartHub account normally has no EXCESS+credit on its API bills, so it
        # takes the "model A" path: allocation × MEASURED generation × an operator-
        # entered rate (never the GMP/VT default). BUT once a VEC bill PDF has been
        # parsed into a settled Bill (kwh_sent_to_grid + solar_credit_usd — see
        # adapters.vec_bill), that bill carries the EXCESS and its OWN net-meter
        # credit rate, exactly like a GMP bill. In that case PREFER the bill: fall
        # through to the GMP utility-bill computation below so the VEC offtaker auto-
        # prices from the bill's own rate (no operator rate needed). GMP accounts are
        # unaffected — _is_sh is False for them.
        with SessionLocal() as _db0:
            _a0 = _db0.get(UtilityAccount, sub.utility_account_id)
            _is_sh = is_smarthub_provider((_a0.provider or "").lower()) if _a0 else False
            _sh_has_bill = False
            if _is_sh:
                # Peek the SAME helper the GMP path uses: a non-None credit_usd means
                # a parsed VEC bill with billable excess exists for this account.
                _cu = _utility_bill_credit(
                    _db0, sub.utility_account_id, target_label=period_label)[1]
                _sh_has_bill = _cu is not None
        # SmartHub with NO parsed bill yet → model-A fallback (operator rate).
        # SmartHub WITH a parsed bill → fall through to the bill-priced GMP block.
        if _is_sh and not _sh_has_bill:
            return _build_smarthub_offtaker_match(sub, operator, warnings)
        # QUARTERLY cadence aggregates the FULL quarter of settled bills (#6). A
        # month-targeted period_label (sheet-tracker backfill) keeps the single-
        # month math; a 'YYYY-Qn' label pins a specific historical quarter.
        _quarterly = ((getattr(sub, "cadence", None) or "monthly") == "quarterly"
                      and (period_label is None or _is_quarter_label(period_label)))
        q = None
        with SessionLocal() as db:
            acct = db.get(UtilityAccount, sub.utility_account_id)
            # Provider-aware label: don't hardcode "GMP " for a VEC/SmartHub account.
            if acct and acct.nickname:
                array_name = acct.nickname
            elif acct:
                _prov = (acct.provider or "").upper() or "GMP"
                array_name = f"{_prov} {acct.account_number}"
            else:
                array_name = None
            if _quarterly:
                q = _utility_bill_credit_quarter(db, sub.utility_account_id,
                                                 target_label=period_label)
                if q is not None:
                    # An INCOMPLETE quarter is held (credit None → delivery skips)
                    # — never billed short with months silently missing.
                    excess_kwh = q["excess_kwh"] if q["complete"] else None
                    credit_usd = q["credit_usd"] if q["complete"] else None
                    credit_rate = q["credit_rate"] if q["complete"] else None
                    start, end, label = q["start"], q["end"], q["label"]
                    rate_source = q["rate_source"]
                else:
                    excess_kwh = credit_usd = credit_rate = None
                    start = end = label = rate_source = None
            else:
                excess_kwh, credit_usd, credit_rate, start, end, label, rate_source = \
                    _utility_bill_credit(db, sub.utility_account_id,
                                         target_label=period_label)
        kwh_source = "utility_bill"
        _share = getattr(sub, "array_share_pct", None)
        # ── No-own-bill FALLBACK (Ford 2026-07-10) ───────────────────────────────
        # An own-meter offtaker whose sub-account has NO settled bill for the
        # period bills their ENTERED share × the group's HOST (master) bill —
        # real captured data, never an estimate — and switches to their own bill
        # automatically once it lands. Monthly only: a quarterly invoice keeps
        # its hold-until-complete semantics (never billed short).
        _fb = None
        if credit_usd is None and not _quarterly and _share:
            from ..models import UtilityAccount as _UAfb
            with SessionLocal() as _dbf:
                _acct2 = _dbf.get(_UAfb, sub.utility_account_id)
                _hid = None
                if _acct2 is not None and _acct2.array_id is not None:
                    _hid = _dbf.execute(
                        select(_UAfb.id).where(
                            _UAfb.array_id == _acct2.array_id,
                            _UAfb.deleted_at.is_(None))
                        .order_by(_UAfb.id)).scalars().first()
                if _hid is not None and _hid != sub.utility_account_id:
                    _h = _utility_bill_credit(_dbf, _hid, target_label=period_label)
                    if _h[1] is not None:      # host bill settled with excess
                        _fb = _h
        if credit_usd is None and _fb is not None:
            # Host-bill fallback: period, rate, and pool all come from the
            # MASTER bill; the offtaker has no own bill this period.
            excess_kwh, credit_usd, credit_rate, start, end, label, rate_source = _fb
            array_kwh = None                  # no OWN bill — own-bill excess stays None
            warnings.append(
                "No settled utility bill on this offtaker's own sub-account yet — "
                "billed as their entered share × the group's master bill for this "
                "period; switches to their own bill automatically once it lands.")
        elif credit_usd is None:
            # No GMP bill with excess generation yet → wait. NEVER substitute gross
            # kWh × a flat rate (that over-charged a banked month $10,659 for ~$2).
            # array_kwh None → delivery skips.
            if _quarterly and q is not None and q["missing_labels"]:
                warnings.append(
                    f"Holding the {q['label']} quarterly invoice — waiting on the "
                    f"utility bill{'s' if len(q['missing_labels']) > 1 else ''} for "
                    f"{', '.join(_month_label_pretty(m) for m in q['missing_labels'])}. "
                    "A quarterly invoice sums all of the quarter's settled bills; a "
                    "missing month is never dropped, estimated, or billed short.")
            else:
                warnings.append(
                    "Waiting on a GMP bill with excess generation for this offtaker — "
                    "the invoice generates from the bill's EXCESS sent to grid once it "
                    "lands; no vendor data, gross kWh, or flat rate is substituted.")
            array_kwh = None
            credit_rate = None
        else:
            array_kwh = excess_kwh          # bill the EXCESS sent to grid, not gross
        # Human-readable coverage note for a quarterly invoice — mark the covered
        # range explicitly, especially when service started mid-quarter.
        period_note = None
        if _quarterly and q is not None and credit_usd is not None:
            _cov = [_month_label_pretty(m) for m in q["covered_labels"]]
            if q["partial_start"]:
                period_note = (f"Quarterly invoice for {q['label']} — covers "
                               f"{', '.join(_cov)} (service began mid-quarter; "
                               "earlier months have no utility bill).")
                warnings.append(period_note)
            else:
                period_note = (f"Quarterly invoice for {q['label']} — sums the "
                               f"{', '.join(_cov)} utility bills.")
        if not pct:
            warnings.append("No allocation % set for this offtaker.")
            pct = 0.0
        # ── Billing basis: THE OFFTAKER'S OWN BILL GOVERNS (Ford 2026-07-10) ────
        # REVERSAL of the 2026-07-01 real_math-wins rule. The sub-client's own
        # utility bill IS GMP's allocation of the net-meter group — its excess,
        # rate, and bill number are billing truth. The operator-entered share
        # exists for the BILL-ACCURACY AUDIT (entered share vs GMP's derived
        # share — reconcile_bills), NOT to move the invoice: editing the share
        # must never change a bill that has its own settled utility bill behind
        # it. real_math (entered share × the group's host pool) bills ONLY as
        # the fallback when the offtaker's own sub-account has no settled bill
        # for the period (the _fb path above put the HOST bill's pool/rate/
        # period into excess_kwh/credit_* and left array_kwh=None).
        # Both figures stay on computed_invoice for the side-by-side audit.
        gmp_credited_kwh = round((array_kwh or 0.0) * pct, 2)
        if _fb is not None and _share:
            _group = excess_kwh               # the host pool the fallback fetched
        elif _share and array_kwh is not None:
            # Group pool resolved for the AUDIT side-figures + derived share.
            # Quarterly needs the host pool summed over the SAME months; the
            # helper returns None unless EVERY covered month has a host bill.
            _group = (_array_group_excess_for_sub(sub, period_labels=q["covered_labels"])
                      if _quarterly and q is not None
                      else _array_group_excess_for_sub(sub, label))
        else:
            _group = None
        # ONE consistent displayed pair (base × share = billed kWh) — the bases
        # must never mix (backlog 2026-07-01: allocation_pct=1.0 rendered next to
        # a customer_kwh that was 99.4% of a DIFFERENT total read as an arithmetic
        # error).
        if array_kwh is not None:
            # Own bill settled → it governs: pct × own-bill excess (pct is 1.0
            # for an own-meter offtaker; the entered share for a host-bound one).
            customer_kwh = gmp_credited_kwh
            _billing_basis = "gmp_credited"
            _base_kwh, _base_pct = array_kwh, pct
        elif _share and _group:
            # No own bill this period → entered share × the group's host pool.
            customer_kwh = round(_share * _group, 2)
            _billing_basis = "real_math"
            _base_kwh, _base_pct = _group, _share
        else:
            customer_kwh = gmp_credited_kwh   # 0.0; has_utility_bill False → skip
            _billing_basis = "gmp_credited"
            _base_kwh, _base_pct = array_kwh, pct
        pricing = resolve_discount_pricing(sub, period_end=end)
        discount = pricing["discount_pct"]
        billing_rate = 1.0 - discount
        # The net rate is the solar CREDIT rate. Precedence for BILL-BOUND offtakers
        # (Ford 2026-07-13):
        #   1. per-offtaker override (sub.net_rate_per_kwh)
        #   2. master solar credit rate (tenant.default_net_rate_per_kwh) when SET
        #   3. THIS offtaker's bound utility sub-account bill EXCESS credit rate
        #      (or a banked-month reference) — custom per offtaker, never a fleet median
        # A quarterly invoice blends the quarter's per-bill rates (Σ credit ÷ Σ excess).
        _blend = (_quarterly and q is not None
                  and len(q.get("covered_labels") or []) > 1)
        _master = pricing.get("tenant_net_rate")
        if pricing["net_source"] == "customer":
            net_rate, net_source = pricing["net_rate"], "customer"
            net_note = pricing.get("net_rate_note")
        elif (getattr(sub, "rate_per_kwh", None) or 0) > 0:
            # Legacy flat $/kWh on a BILL-BOUND offtaker: the flat rate IS the
            # agreed price — bill it (no re-discount unless one is explicitly
            # set). Without this, the sub billed the BILL's credit rate paired
            # with the flat rule's zero discount — neither semantic (caught at
            # 800-offtaker scale; prod carried 0 such subs when this landed).
            net_rate, net_source = float(sub.rate_per_kwh), "legacy_flat_customer"
            net_note = "the customer's legacy flat $/kWh — the agreed price"
            if sub.discount_pct is None or not (0 <= sub.discount_pct < 1):
                discount = 0.0
                billing_rate = 1.0
        elif _master is not None and _master > 0:
            # Master rate set → every offtaker without a per-offtaker override
            # shares this fleet rate (overrides each sub-account bill rate).
            net_rate, net_source = float(_master), "global"
            net_note = "your master solar credit rate"
        elif rate_source == "reference":
            net_rate, net_source = (credit_rate or 0.0), "gmp_credit_reference"
            net_note = ("the solar credit rate for comparable months — this period's "
                        "excess was banked (not cashed), trued up annually")
            if _blend:
                net_note += " (blended across the quarter's bills)"
        else:
            # Master blank → THIS offtaker's own utility sub-account bill rate.
            net_rate, net_source = (credit_rate or 0.0), "gmp_bill_credit"
            net_note = ("the quarter's blended EXCESS + SOLCRED net-metering credit"
                        if _blend else
                        "this offtaker's utility bill EXCESS + SOLCRED credit rate")
        # The DEFAULT rate this offtaker would price at with NO per-offtaker override:
        # master (if set) else this offtaker's own bill/reference rate. Surfaced so the
        # offtaker editor can show "default: $X — from their utility bill" vs master.
        if _master is not None and _master > 0:
            default_net_rate = float(_master)
            default_net_source = "global"
            default_net_note = "your master solar credit rate"
        elif rate_source == "reference":
            default_net_rate = credit_rate
            default_net_source = "gmp_credit_reference"
            default_net_note = ("the solar credit rate for comparable months — this "
                                "period's excess was banked (not cashed), trued up annually")
            if _blend:
                default_net_note += " (blended across the quarter's bills)"
        elif credit_rate is not None:
            default_net_rate = credit_rate
            default_net_source = "gmp_bill_credit"
            default_net_note = ("the quarter's blended EXCESS + SOLCRED net-metering credit"
                                if _blend else
                                "this offtaker's utility bill EXCESS + SOLCRED credit rate")
        else:
            default_net_rate = None
            default_net_source = None
            default_net_note = None
        period = Period(
            month=label, start=start, end=end,
            array_kwh=(_base_kwh or 0.0), customer_kwh=customer_kwh,
            tariff=net_rate, adder=0.0,
        )
        computed = compute_invoice(customer_kwh, net_rate, 0.0,
                                   billing_rate, "percent_of_array", None)
        computed["invoice_number"] = (label if (_quarterly and label)
                                      else (end.strftime("%Y-%m") if end else label))
        computed["period_start"] = start.isoformat() if start else None
        computed["period_end"] = end.isoformat() if end else None
        computed["month"] = label
        computed["project_total_kwh"] = (_base_kwh or 0.0)
        computed["array_kwh"] = (_base_kwh or 0.0)
        computed["excess_kwh"] = (_base_kwh or 0.0)
        # The offtaker's OWN bill excess — kept distinct from the billed base so
        # the two never read as the same number when the basis is real_math.
        computed["own_bill_excess_kwh"] = array_kwh
        computed["solar_credit_usd"] = credit_usd
        computed["net_rate_per_kwh"] = round(net_rate, 6)
        computed["discount_pct"] = round(discount, 6)
        computed["effective_rate_per_kwh"] = round(net_rate * billing_rate, 6)
        computed["net_rate_source"] = net_source
        computed["net_rate_note"] = net_note
        # The bill-derived DEFAULT (what the rate is WITHOUT a per-customer
        # override) + its honest source — so the editable rate field can show it
        # as the default beneath an override, and never mislabels a banked
        # reference rate as "from your GMP bill".
        computed["default_net_rate_per_kwh"] = (round(default_net_rate, 6)
                                                if default_net_rate is not None else None)
        computed["default_net_rate_source"] = default_net_source
        computed["default_net_rate_note"] = default_net_note
        computed["discount_source"] = pricing["discount_source"]
        computed["rate_per_kwh"] = round(net_rate * billing_rate, 6)
        computed["rate_source"] = pricing["discount_source"]
        computed["kwh_source"] = kwh_source           # always 'utility_bill' here
        # has_data flag the empty-skip path checks (None = nothing billable).
        computed["has_utility_bill"] = credit_usd is not None
        # Own-bill vs entered-share provenance so the invoice/UI can show BOTH the
        # amount we bill and the audit expectation, and flag the gap.
        computed["billing_basis"] = _billing_basis
        # None (not 0) when the offtaker has no own bill this period — a zero here
        # would read as "GMP credited nothing" instead of "no bill yet".
        computed["gmp_credited_kwh"] = (gmp_credited_kwh
                                        if array_kwh is not None else None)
        computed["array_share_pct"] = _share
        computed["array_group_excess_kwh"] = _group
        computed["realmath_kwh"] = (round(_share * _group, 2)
                                    if (_share and _group) else None)
        # GMP's DERIVED share — own-bill excess ÷ the group's host pool. This is
        # "the net meter group percentage pulled from the bill" (Ford 2026-07-10):
        # what the offtaker's own bill says their share actually was this period.
        computed["derived_share_pct"] = (round(array_kwh / _group, 6)
                                         if (array_kwh and _group) else None)
        # Quarterly coverage provenance (renderers mark the covered range).
        computed["billing_cadence"] = "quarterly" if _quarterly else "monthly"
        if _quarterly and q is not None:
            computed["period_months"] = q["covered_labels"]
            computed["period_missing_months"] = q["missing_labels"]
            computed["quarter_month_breakdown"] = q["months"]
        computed["period_note"] = period_note
        return BillingMatch(
            matched=True, confidence=1.0, source="manual", data_sheet=None,
            customer={"name": sub.customer_name, "email": sub.client_email},
            allocation_pct=_base_pct,
            billing_rate=billing_rate, billing_model="percent_of_array",
            periods=[period], latest_period=period,
            template={"title": "Invoice - Solar Power Generation", "operator": operator},
            computed_invoice=computed,
            project_totals={
                "total_array_kwh": (_base_kwh or 0.0),
                "total_customer_kwh": customer_kwh,
                "array_name": array_name,
            },
            warnings=warnings,
        )

    # Multi-array path: an offtaker owning a share of SEVERAL arrays. Sum each
    # array's (period kWh × that array's pct) into one combined invoice, with a
    # per-array breakdown line. Falls back to the single array_id path below.
    allocs = _normalized_allocations(sub)
    if allocs:
        from ..db import SessionLocal
        from ..models import Array
        from .matcher import Period, compute_invoice
        breakdown: list[dict] = []
        total_customer_kwh = 0.0
        total_array_kwh = 0.0
        starts: list[date] = []
        ends: list[date] = []
        labels: list[str] = []
        sources: set[str] = set()
        with SessionLocal() as db:
            for al in allocs:
                aid = al["array_id"]
                apct = al["allocation_pct"]
                arr = db.get(Array, aid)
                a_kwh, a_start, a_end, a_label, a_src = _array_period_kwh_sourced(db, aid)
                if a_kwh is None:
                    warnings.append(f"No generation data yet for "
                                    f"{(arr.name if arr else 'array ' + str(aid))} — "
                                    f"its share shows $0 until production lands.")
                    a_kwh = 0.0
                cust_kwh = round(a_kwh * apct, 2)
                total_customer_kwh += cust_kwh
                total_array_kwh += a_kwh
                if a_start: starts.append(a_start)
                if a_end: ends.append(a_end)
                if a_label: labels.append(a_label)
                if a_src: sources.add(a_src)
                breakdown.append({
                    "array_id": aid,
                    "array_name": arr.name if arr else f"Array {aid}",
                    "array_kwh": round(a_kwh, 1),
                    "allocation_pct": apct,
                    "customer_kwh": cust_kwh,
                })
        total_customer_kwh = round(total_customer_kwh, 2)
        total_array_kwh = round(total_array_kwh, 1)
        start = min(starts) if starts else None
        end = max(ends) if ends else None
        # Common label when all arrays share the same period; else the latest.
        label = labels[-1] if labels else None
        if len(set(labels)) == 1:
            label = labels[0] if labels else None
        elif len(set(labels)) > 1:
            warnings.append("Selected arrays have different latest billing periods; "
                            "invoice uses the combined range.")
        kwh_source = "+".join(sorted(sources)) if sources else None

        pricing = resolve_discount_pricing(sub, period_end=end)
        net_rate = pricing["net_rate"]
        discount = pricing["discount_pct"]
        billing_rate = 1.0 - discount
        period = Period(
            month=label, start=start, end=end,
            array_kwh=total_array_kwh, customer_kwh=total_customer_kwh,
            tariff=net_rate, adder=0.0,
        )
        computed = compute_invoice(total_customer_kwh, net_rate, 0.0,
                                   billing_rate, "percent_of_array", None)
        computed["invoice_number"] = end.strftime("%Y-%m") if end else label
        computed["period_start"] = start.isoformat() if start else None
        computed["period_end"] = end.isoformat() if end else None
        computed["month"] = label
        computed["project_total_kwh"] = total_array_kwh
        computed["array_kwh"] = total_array_kwh
        computed["array_breakdown"] = breakdown   # one line per array
        computed["net_rate_per_kwh"] = round(net_rate, 6)
        computed["discount_pct"] = round(discount, 6)
        computed["effective_rate_per_kwh"] = pricing["effective_rate"]
        computed["net_rate_source"] = pricing["net_source"]
        computed["net_rate_note"] = pricing.get("net_rate_note")
        computed["discount_source"] = pricing["discount_source"]
        computed["rate_per_kwh"] = pricing["effective_rate"]
        computed["rate_source"] = pricing["discount_source"]
        computed["kwh_source"] = kwh_source
        return BillingMatch(
            matched=True, confidence=1.0, source="manual", data_sheet=None,
            customer={"name": sub.customer_name, "email": sub.client_email},
            allocation_pct=None,
            billing_rate=billing_rate, billing_model="percent_of_array",
            periods=[period], latest_period=period,
            template={"title": "Invoice - Solar Power Generation", "operator": operator},
            computed_invoice=computed,
            project_totals={
                "total_array_kwh": total_array_kwh,
                "total_customer_kwh": total_customer_kwh,
                "array_name": ", ".join(b["array_name"] for b in breakdown),
                "array_breakdown": breakdown,
            },
            warnings=warnings,
        )

    if sub.array_id is not None:
        with SessionLocal() as db:
            arr = db.get(Array, sub.array_id)
            array_name = arr.name if arr else None
            array_kwh, start, end, label, kwh_source = _array_period_kwh_sourced(
                db, sub.array_id)
    if array_kwh is None:
        warnings.append("No generation data for this array yet — invoice shows $0 "
                        "until production lands.")
        array_kwh = 0.0
    if not pct:
        warnings.append("No allocation % set for this manual customer.")
        pct = 0.0

    customer_kwh = round(array_kwh * pct, 2)
    # Discount pricing: invoice = kWh × net_rate × (1 − discount). Resolve net
    # rate + discount with per-customer override → operator global → defaults
    # (10% off the VT net rate). compute_invoice's billing_rate = (1 − discount),
    # so amount_owed == kWh × net_rate × (1−discount) and solar_savings == the
    # discount the customer receives. Never fabricated — $0 when no generation.
    pricing = resolve_discount_pricing(sub, period_end=end)
    net_rate = pricing["net_rate"]
    discount = pricing["discount_pct"]
    billing_rate = 1.0 - discount
    period = Period(
        month=label, start=start, end=end,
        array_kwh=array_kwh, customer_kwh=customer_kwh,
        tariff=net_rate, adder=0.0,
    )
    computed = compute_invoice(customer_kwh, net_rate, 0.0,
                               billing_rate, "percent_of_array", None)
    computed["invoice_number"] = end.strftime("%Y-%m") if end else label
    computed["period_start"] = start.isoformat() if start else None
    computed["period_end"] = end.isoformat() if end else None
    computed["month"] = label
    computed["project_total_kwh"] = array_kwh
    computed["array_kwh"] = array_kwh
    # Discount-model fields for the UI/invoice (auditable savings story):
    computed["net_rate_per_kwh"] = round(net_rate, 6)
    computed["discount_pct"] = round(discount, 6)
    computed["effective_rate_per_kwh"] = pricing["effective_rate"]
    computed["net_rate_source"] = pricing["net_source"]
    computed["net_rate_note"] = pricing.get("net_rate_note")
    computed["discount_source"] = pricing["discount_source"]
    # Back-compat: keep rate_per_kwh = the effective billed rate + a coarse source.
    computed["rate_per_kwh"] = pricing["effective_rate"]
    computed["rate_source"] = pricing["discount_source"]
    # Where the produced-kWh number came from: 'gmp_api' (authoritative GMP
    # daily-read) | 'daily_csv' (DailyGeneration/Bill fallback) | None (no data).
    computed["kwh_source"] = kwh_source

    return BillingMatch(
        matched=True,
        confidence=1.0,
        source="manual",
        data_sheet=None,
        customer={"name": sub.customer_name, "email": sub.client_email},
        allocation_pct=pct,
        billing_rate=billing_rate,
        billing_model="percent_of_array",
        periods=[period],
        latest_period=period,
        template={"title": "Invoice - Solar Power Generation", "operator": operator},
        computed_invoice=computed,
        project_totals={
            "total_array_kwh": array_kwh,
            "total_customer_kwh": customer_kwh,
            "array_name": array_name,
        },
        warnings=warnings,
    )


def _effective_template_row(db, sub, force: bool = False):
    """The invoice template to render this offtaker's invoice from. A PER-OFFTAKER
    template (OfftakerSubscriptionTemplate, keyed by subscription) OVERRIDES the
    tenant-wide default (OfftakerInvoiceTemplate); each respects its own `enabled`
    gate unless `force` (preview). Precedence: per-offtaker → tenant default → None
    (fall back to the standard branded invoice). Both rows share the same shape
    (html / file_bytes / enabled), so the two render paths use this interchangeably."""
    from ..models import OfftakerInvoiceTemplate, OfftakerSubscriptionTemplate
    sid = getattr(sub, "id", None)
    if sid is not None:
        st = db.execute(select(OfftakerSubscriptionTemplate).where(
            OfftakerSubscriptionTemplate.subscription_id == sid)).scalars().first()
        if st and (force or st.enabled) and (st.html or st.file_bytes):
            return st
    tid = getattr(sub, "tenant_id", None)
    if tid is not None:
        tpl = db.execute(select(OfftakerInvoiceTemplate).where(
            OfftakerInvoiceTemplate.tenant_id == tid)).scalars().first()
        if tpl and (force or tpl.enabled) and (tpl.html or tpl.file_bytes):
            return tpl
    return None


def _render_from_operator_template(match, sub, out_path, force: bool = False) -> bool:
    """If this offtaker (or, as a fallback, their tenant) has an ENABLED invoice
    template, render the invoice in THAT format and write it to out_path. Returns True
    on success, False to fall back to the standard PDF. NEVER raises — a bad template
    can't break a real send. force=True ignores the enabled flag (preview compare)."""
    if sub is None or not getattr(sub, "tenant_id", None):
        return False
    try:
        from ..db import SessionLocal
        from ..models import Tenant
        from .template_render import render_template_pdf, build_token_context
        with SessionLocal() as db:
            tpl = _effective_template_row(db, sub, force)   # per-offtaker → tenant → None
            if not tpl or not tpl.html:
                return False
            tenant = db.get(Tenant, sub.tenant_id)
            ctx = build_token_context(match, sub, tenant)
            pdf = render_template_pdf(tpl.html, ctx)
        out_path.write_bytes(pdf)
        return True
    except Exception:  # noqa: BLE001 — fall back to the standard invoice, never break a send
        logger.exception("operator invoice-template render failed; using standard invoice")
        return False


def _render_from_operator_template_repro(match, sub, out_path, force: bool = False) -> bool:
    """Pixel-perfect path for offtakers WITHOUT their own workbook: reproduce the
    invoice in the operator's ENABLED Excel template — write THIS offtaker's
    computed values into the template's mapped display cells + swap their name, and
    headless-render (Gotenberg). FAIL-CLOSED on amount + identity (template_repro),
    so a mismatched fill falls back instead of mailing the template's sample numbers.
    Gated on REPRO_ENABLED + an enabled xlsx template + a renderer. Never raises."""
    if sub is None or getattr(sub, "source_workbook", None):
        return False                                  # workbook offtakers: _render_from_repro handles
    try:
        from ..db import SessionLocal
        from .repro import repro_enabled
        if not repro_enabled():
            return False
        from .repro import render as _repro_render
        if not _repro_render.renderer_available():
            return False
        from .repro.template_repro import reproduce_in_template
        with SessionLocal() as db:
            tpl = _effective_template_row(db, sub, force)   # per-offtaker → tenant → None
            if not tpl or not tpl.file_bytes:
                return False
            fb = bytes(tpl.file_bytes)
        if fb[:4] != b"PK\x03\x04":                   # xlsx only (PDF/Word/HTML → token-HTML path)
            return False
        name = ((match.customer or {}).get("name")
                or getattr(sub, "customer_name", None) or "Offtaker")
        res = reproduce_in_template(fb, offtaker_match=match, customer_name=name)
        if res and res.pdf and res.ok is True:
            out_path.write_bytes(res.pdf)
            logger.info("repro: pixel-perfect operator-template invoice for sub %s",
                        getattr(sub, "id", "?"))
            return True
        return False
    except Exception:  # noqa: BLE001 — never break a send
        logger.exception("operator-template repro failed; falling back")
        return False


def _render_from_repro(match, sub, out_path) -> bool:
    """Pixel-perfect path: fill the operator's OWN workbook for this period and
    render it to PDF with the headless engine (Gotenberg). It IS their file, so
    the PDF matches their format down to the pixel.

    Gated on REPRO_ENABLED and workbook subscriptions (a stored source_workbook);
    requires a configured renderer. NEVER raises — returns False so the caller
    falls back to the operator-template / standard invoice and a render outage or
    odd workbook can't break a send."""
    try:
        from .repro import repro_enabled
        if not repro_enabled():
            return False
        if sub is None or not getattr(sub, "source_workbook", None):
            return False
        from .repro import render as _repro_render
        if not _repro_render.renderer_available():
            return False
        from .repro.pipeline import reproduce_for_subscription
        # verify=False → skip the AI vision call at send time; the deterministic
        # fail-closed numeric guard runs. Ship ONLY when positively verified
        # (ok is True) — an unverifiable/unreadable render falls back to standard.
        res = reproduce_for_subscription(sub, period_data=match.latest_period, verify=False)
        if res.pdf and res.ok is True:
            # A multi-sheet workbook renders every sheet (Data ledger, Trends, annual
            # True-Up); the offtaker should receive ONLY their invoice page. Trim to it
            # deterministically by this invoice's expected total (best-effort, never blanks).
            expected = (match.computed_invoice or {}).get("amount_owed")
            pdf = _repro_render.trim_pdf_to_invoice_page(res.pdf, expected)
            pdf = _repro_render.center_pdf_to_content(pdf)   # crop to content → centered, no waste
            out_path.write_bytes(pdf)
            logger.info("repro: pixel-perfect invoice PDF for sub %s via %s "
                        "(verified, rounds=%s)", getattr(sub, "id", "?"),
                        res.backend, res.rounds)
            return True
        logger.warning("repro: not verified for sub %s (ok=%s) — falling back to "
                       "standard invoice", getattr(sub, "id", "?"), getattr(res, "ok", None))
        return False
    except Exception:  # noqa: BLE001 — never break a send
        logger.exception("repro render failed; falling back to template/standard invoice")
        return False


def generate_files(match: BillingMatch, formats: list[str], include_summary: bool,
                   out_dir: pathlib.Path, invoice_date: Optional[date] = None,
                   peer: Optional[dict] = None, sub=None,
                   gmp_pdf_override: Optional[bytes] = None,
                   pay_url: Optional[str] = None) -> list[pathlib.Path]:
    """Render the chosen attachment files into out_dir. Returns their paths.

    `gmp_pdf_override` (bytes) is a GMP bill PDF to attach for THIS render only —
    passed by an approved draft so the manually-attached bill is scoped to this
    one send and never persisted onto the sub to ride future periods' invoices.
    When absent, a `sub` carrying a stored GMP invoice PDF still rides it (Paul's
    dormant hook). `sub`/`gmp_pdf_override` are optional, keeping the signature
    back-compatible.

    `pay_url` (V2 offtaker pay-link) is stamped onto the standard PDF/XLSX
    invoice when present so the offtaker can pay online. Repro / operator-
    template paths keep their own layout (pay CTA still rides the email).
    """
    invoice_date = invoice_date or date.today()
    safe = (match.customer.get("name") or "customer").replace(" ", "_").replace("/", "-")
    inv_no = (match.computed_invoice or {}).get("invoice_number") or invoice_date.strftime("%Y-%m")
    stem = f"{safe}_{inv_no}"
    # GMP bill rides the email named like the invoice: a self-describing
    # gmp_utility_bill_<offtaker>_<period>.pdf (mirrors reports.js gmpBillFilename),
    # not the operator's raw upload name.
    import re as _re
    _gmp_slug = (_re.sub(r"[^a-z0-9]+", "_",
                         (match.customer.get("name") or "offtaker").lower()).strip("_")
                 or "offtaker")
    gmp_name = f"gmp_utility_bill_{_gmp_slug}_{inv_no}.pdf"
    # VEC/SmartHub-bound offtakers ride their OWN utility bill under a parallel,
    # self-describing name (mirrors gmp_name / reports.js gmpBillFilename).
    vec_name = f"vec_utility_bill_{_gmp_slug}_{inv_no}.pdf"
    paths: list[pathlib.Path] = []
    fmts = [f.lower() for f in (formats or ["pdf"])]
    if "pdf" in fmts:
        inv_pdf = out_dir / f"{stem}_invoice.pdf"
        # Invoice PDF, best fidelity first, each step falling back to the next so a
        # failure never breaks a send:
        #   1. repro — fill their OWN workbook + headless-render (workbook subs);
        #   2. operator-template repro — write this offtaker's values into the
        #      operator's ENABLED xlsx template + headless-render (non-workbook subs);
        #   both pixel-perfect, flagged, fail-closed.
        #   3. operator token-HTML template; 4. the standard branded PDF.
        if not (_render_from_repro(match, sub, inv_pdf)
                or _render_from_operator_template_repro(match, sub, inv_pdf)
                or _render_from_operator_template(match, sub, inv_pdf)):
            invoice_mod.render_invoice_pdf(
                match, inv_pdf, invoice_date=invoice_date, pay_url=pay_url)
        paths.append(inv_pdf)
    if "xlsx" in fmts:
        paths.append(invoice_mod.render_invoice_xlsx(
            match, out_dir / f"{stem}_invoice.xlsx", invoice_date=invoice_date,
            pay_url=pay_url))
    if include_summary:
        if "pdf" in fmts:
            paths.append(summary_mod.render_summary_pdf(
                match, out_dir / f"{stem}_summary.pdf", peer=peer))
        elif "xlsx" in fmts:
            paths.append(summary_mod.render_summary_xlsx(
                match, out_dir / f"{stem}_summary.xlsx", peer=peer))
    # Utility-bill PDF attachment (the auto_attach_gmp toggle — conceptually
    # "attach the offtaker's bound utility bill", provider-aware). Two sources,
    # manual takes precedence:
    #   1. sub.gmp_invoice_pdf — a manually uploaded PDF (Paul's GMP fallback).
    #   2. auto-attach — when sub.auto_attach_gmp is on AND no manual PDF, look up
    #      the captured bill PDF for the bound account + period via the read seam.
    #      For a GMP-bound offtaker that's the GMP bill; for a VEC/SmartHub-bound
    #      one it's the VEC bill (persisted PDF bytes on Bill.pdf_bytes). Returns
    #      nothing until ingestion persists durable bytes (never fabricated).
    _manual_pdf = gmp_pdf_override
    if _manual_pdf is None and sub is not None and getattr(sub, "gmp_invoice_pdf", None):
        _manual_pdf = bytes(sub.gmp_invoice_pdf)
    if _manual_pdf:
        gmp_path = out_dir / gmp_name
        gmp_path.write_bytes(_manual_pdf)
        paths.append(gmp_path)
    elif sub is not None and getattr(sub, "auto_attach_gmp", False):
        try:
            from ..reports import gmp_bill_pdf_read as gbp
            ci = match.computed_invoice or {}
            ps = _parse_iso_date(ci.get("period_start"))
            pe = _parse_iso_date(ci.get("period_end"))
            uaid = getattr(sub, "utility_account_id", None)
            found = None
            out_name = gmp_name
            # Provider-aware: a VEC/SmartHub-bound offtaker attaches its VEC bill
            # (the invoice's actual source), not the GMP read seam (which finds
            # nothing for a SmartHub account). Resolve the bound account's provider.
            if uaid is not None and _is_smarthub_bound(sub):
                found = gbp.get_vec_bill_pdf_for_account(uaid, ps, pe)
                out_name = vec_name
            elif uaid is not None:
                # GMP-bound (or legacy) offtaker → the GMP bill for this account.
                found = gbp.get_bill_pdf_for_account(uaid, ps, pe)
            elif getattr(sub, "array_id", None) is not None:
                # Legacy array-based subscription with no bound account → GMP.
                found = gbp.get_bill_pdf_for_period(sub.array_id, ps, pe)
            if found and found.get("bytes"):
                bill_path = out_dir / out_name
                bill_path.write_bytes(found["bytes"])
                paths.append(bill_path)
        except Exception:  # noqa: BLE001 — provisional seam must never break send
            logger.warning("auto-attach utility bill lookup failed for sub %s",
                           getattr(sub, "id", "?"), exc_info=True)
    return paths


def _is_smarthub_bound(sub) -> bool:
    """True if the subscription's bound utility account is a VEC/SmartHub co-op.

    Reads the bound UtilityAccount's provider. Best-effort + fail-safe: any error
    (no account, missing column) returns False so the caller falls back to the GMP
    path — never breaks a send.
    """
    uaid = getattr(sub, "utility_account_id", None)
    if uaid is None:
        return False
    try:
        from ..db import SessionLocal
        from ..models import UtilityAccount
        from ..adapters.smarthub import is_smarthub_provider
        with SessionLocal() as db:
            ua = db.get(UtilityAccount, uaid)
            return bool(ua and is_smarthub_provider((ua.provider or "").lower()))
    except Exception:  # noqa: BLE001
        return False


def _parse_iso_date(s) -> Optional[date]:
    """Parse a 'YYYY-MM-DD'(...) string to a date, or None."""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


# ─── recipients ─────────────────────────────────────────────────────────────

def resolve_recipients(sub, tenant) -> tuple[list[str], list[str], list[str]]:
    """Return (to, cc, problems) from the send-mode slider.

    to_me     → operator only.
    to_client → client (+ cc_emails).
    to_both   → client primary, operator cc'd (+ cc_emails).
    """
    op = sub.operator_email or getattr(tenant, "contact_email", None)
    client = sub.client_email
    extra = [e.strip() for e in (sub.cc_emails or "").split(",") if e.strip()]
    problems: list[str] = []
    mode = sub.send_mode or "to_me"
    if mode == "to_me":
        to = [op] if op else []
        cc: list[str] = []
        if not op:
            problems.append("No operator email on file.")
    elif mode == "to_client":
        to = [client] if client else []
        cc = extra
        if not client:
            problems.append("Send mode is 'to client' but no client email is set.")
    else:  # to_both
        # No client → NO primary recipient. Do NOT fall back to operator-only:
        # that silently sends the operator a copy while the customer is never
        # invoiced, and the caller believed the customer was billed (#28). Leaving
        # `to` empty makes deliver_subscription surface the problem and abort.
        to = [client] if client else []
        cc = ([op] if (op and client) else []) + extra
        if not client:
            problems.append("Send mode is 'to both' but no client email is set.")
    # de-dup, keep order
    seen: set[str] = set()
    to = [x for x in to if x and not (x in seen or seen.add(x))]
    cc = [x for x in cc if x and x not in to and not (x in seen or seen.add(x))]
    return to, cc, problems


# ─── email ──────────────────────────────────────────────────────────────────

def _b64(path: pathlib.Path) -> dict:
    return {"filename": path.name,
            "content": base64.b64encode(path.read_bytes()).decode()}


def _offtaker_email_fields(tenant_id) -> dict:
    """One fetch of the tenant fields the offtaker invoice email letter needs:
    the mass template (subject/body), the shared sign-off, and the operator
    identity. Empty dict when the tenant can't be resolved."""
    from ..db import SessionLocal
    from ..models import Tenant
    if not tenant_id:
        return {}
    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if not t:
            return {}
        return {
            "subject_t": getattr(t, "offtaker_email_subject_template", None),
            "body_t": getattr(t, "offtaker_email_body_template", None),
            "signoff_t": getattr(t, "email_signoff", None),
            "tenant_name": (t.company_name or t.operator_name or t.name),
            "tenant_email": (t.contact_email or ""),
            "signoff_name": (t.send_from_name or t.operator_name),
        }


def _attachments_line(has_gmp: bool, has_summary: bool) -> str:
    """The {{attachments_line}} prose — only ever claims files that are there."""
    extras = []
    if has_gmp:
        extras.append("the GMP source bill behind it (so you can see exactly "
                      "how the amount was calculated)")
    if has_summary:
        extras.append("a production summary")
    return ("Your full invoice is attached"
            + (", along with " + " and ".join(extras) if extras else "") + ".")


def _email_html(match: BillingMatch, sub, is_test: bool,
                note: Optional[str] = None,
                attachment_names: Optional[list[str]] = None,
                pay_url: Optional[str] = None) -> tuple[str, str, str]:
    from ..email_templates import (
        DEFAULT_OFFTAKER_SUBJECT_TEMPLATE, DEFAULT_OFFTAKER_BODY_TEMPLATE,
        build_offtaker_context, render_merge, html_to_text,
    )
    inv = match.computed_invoice or {}
    cust = match.customer.get("name") or sub.customer_name or "your array"
    period = ""
    if inv.get("period_start") and inv.get("period_end"):
        period = f"{inv['period_start']} → {inv['period_end']}"
    amount = inv.get("amount_owed")
    amount_str = f"${amount:,.2f}" if isinstance(amount, (int, float)) else "—"
    from ..email_skin import render_email_skin, render_email_skin_text
    kwh = (inv.get("kwh") or 0)
    test_banner = (
        '<p style="background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.35);'
        'color:#92400e;padding:10px 14px;border-radius:10px;margin:0 0 16px;font-size:13px;'
        'line-height:1.45;">'
        '<b>Test send</b> — this went to you, not the customer. '
        'The Pay button below is real (same as offtakers will see).</p>' if is_test else "")

    # ── The LETTER: the tenant's mass template, rendered per offtaker ────────
    # (Ford, 2026-07-03: "the email should automatically say hi <offtaker name>
    # and be a little longer" — the letter is now merge-tag templated, editable
    # in the AO email studio, with the same engine as the NEPOOL customizer.)
    fields = _offtaker_email_fields(getattr(sub, "tenant_id", None))
    operator = fields.get("tenant_name") or "your solar provider"
    if attachment_names is not None:
        # Real send: derive from the actual files going out — never overclaim.
        _names = [n.lower() for n in attachment_names]
        has_gmp_att = any("gmp" in n for n in _names)
        has_summary_att = any("summary" in n for n in _names)
    else:
        # Preview path (no file list yet): mirror the sub's settings, same as
        # the dashboard's draft preview does.
        has_gmp_att = (getattr(sub, "auto_attach_gmp", True) is not False
                       or bool(getattr(sub, "gmp_invoice_pdf", None)))
        has_summary_att = getattr(sub, "include_summary", False) is True
    ctx = build_offtaker_context(
        offtaker_name=cust,
        tenant_name=operator,
        tenant_email=fields.get("tenant_email", ""),
        period=(period or "the latest period"),
        period_start=(inv.get("period_start") or ""),
        period_end=(inv.get("period_end") or ""),
        kwh=f"{kwh:,.0f} kWh",
        amount=amount_str,
        invoice_number=str(inv.get("invoice_number") or ""),
        attachments_line=_attachments_line(has_gmp_att, has_summary_att),
        signoff_template=fields.get("signoff_t"),
        tenant_signoff_name=fields.get("signoff_name"),
    )
    subj_t = (fields.get("subject_t") or "").strip() or DEFAULT_OFFTAKER_SUBJECT_TEMPLATE
    subject = render_merge(subj_t, ctx)
    if not ctx["invoice_number"]:
        subject = subject.replace(" ()", "")   # default template's empty-number parens

    def _row(label, val, strong=False):
        pad = "10px" if strong else "6px"
        # Day-skin emerald for the money figure (matches the redesigned invoice);
        # the old #3fd68a mint washed out on the light card.
        # Sky system: blue-is-good (#2196F3) — same as fleet health / inverter cards
        valstyle = "font-weight:800;color:#1976D2;" if strong else "color:#0E1420;"
        return (f'<tr><td style="padding:{pad} 0;color:#4C596B;font-size:13px;">{label}</td>'
                f'<td style="padding:{pad} 0;text-align:right;font-size:14px;{valstyle}">{val}</td></tr>')

    # The operator's edited note (Paul's "edit a pre-written email"), shown above
    # the figures. Plain text → escaped + newlines to <br> so it renders safely.
    note_html = ""
    note_text = ""
    if note and note.strip():
        import html as _html
        safe = _html.escape(note.strip()).replace("\n", "<br>")
        note_html = (f'<div style="font-size:14px;line-height:1.55;margin:0 0 16px;">'
                     f'{safe}</div>')
        note_text = note.strip() + "\n\n"

    # No per-draft note → the tenant's mass template letter (default: a warm
    # personalized "Hi <first name>" letter). A note REPLACES the letter for
    # that one send — the operator's explicit words always win.
    letter_html = ("" if note_html
                   else render_merge((fields.get("body_t") or "").strip()
                                     or DEFAULT_OFFTAKER_BODY_TEMPLATE, ctx))
    # Budget billing shows TWO distinct figures — the calculated solar credit value
    # AND the fixed budgeted amount actually billed; otherwise just the one amount due.
    budget_override = bool(inv.get("budget_override"))
    credit_val = inv.get("solar_credit_value")
    credit_str = f"${credit_val:,.2f}" if isinstance(credit_val, (int, float)) else amount_str
    if budget_override and isinstance(credit_val, (int, float)):
        amount_rows_html = (
            _row("Solar credit value due", credit_str)
            + _row("Budgeted amount", amount_str, strong=True)
        )
        amount_rows_text = f"Solar credit value due: {credit_str}\nBudgeted amount: {amount_str}\n"
    else:
        amount_rows_html = _row("Solar credit value due", amount_str, strong=True)
        amount_rows_text = f"Solar credit value due: {amount_str}\n"
    figures_table = (
        f'<table width="100%" style="font-size:14px;border-collapse:collapse;margin-top:8px;">'
        f'{_row("Billing period", period or "—")}'
        f'{_row("Your production", f"{kwh:,.0f} kWh")}'
        f'{amount_rows_html}'
        f"</table>"
    )
    # V2 pay-link CTA — lead with the money action (sky redesign). Never
    # fabricate a dead button; only render when delivery minted a real session.
    pay_cta_html = ""
    pay_cta_text = ""
    skin_cta = None
    if pay_url:
        import html as _html_pay
        safe_url = _html_pay.escape(pay_url, quote=True)
        # Pay block: sky pastel-blue glass (matches fleet health / inverter cards
        # — --sky-pastel-blue #D9E7FB, --sky-primary #2196F3). No green.
        pay_cta_html = (
            f'<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            f'style="margin:0 0 22px;border-collapse:separate;">'
            f'<tr><td align="center" bgcolor="#D9E7FB" '
            f'style="background:linear-gradient(180deg,#EAF4FD 0%,#D9E7FB 100%);'
            f'background-color:#D9E7FB;border:1px solid rgba(33,150,243,.28);'
            f'border-radius:16px;padding:20px 18px 16px;">'
            f'<div style="font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;'
            f'color:#1976D2;margin:0 0 6px;">Amount due</div>'
            f'<div style="font-size:32px;font-weight:800;letter-spacing:-.02em;color:#0E1420;'
            f'line-height:1.1;margin:0 0 14px;">{amount_str}</div>'
            f'<a href="{safe_url}" '
            f'style="display:inline-block;background:linear-gradient(180deg,#42A5F5,#2196F3);'
            f'color:#ffffff;padding:14px 28px;border-radius:12px;text-decoration:none;'
            f'font-weight:800;font-size:16px;box-shadow:0 8px 24px -8px rgba(33,150,243,.55);">'
            f'Pay invoice securely →</a>'
            f'<div style="font-size:12px;color:#4C596B;margin-top:12px;line-height:1.4;">'
            f'Secure card payment · due within 28 days · powered by Stripe</div>'
            f'</td></tr></table>'
        )
        pay_cta_text = f"\nPay online ({amount_str}): {pay_url}\n"
        skin_cta = {"label": f"Pay {amount_str}", "url": pay_url}
    if note_html:
        # Per-draft note: the operator wrote this send's letter — keep the
        # legacy composition (note + figures + attachment line) exactly.
        body_html = (
            f"{test_banner}{pay_cta_html}{note_html}{figures_table}"
            f'<p style="margin-top:18px;">The full invoice'
            f'{" and performance summary are" if sub.include_summary else " is"} attached.</p>'
        )
        body_text = (
            f"{note_text}"
            f"Solar credit invoice for {cust}\n\n"
            f"Billing period: {period or '—'}\n"
            f"Your production: {kwh:,.0f} kWh\n"
            f"{amount_rows_text}{pay_cta_text}\n"
            f"The full invoice{' and performance summary are' if sub.include_summary else ' is'} attached.\n\n"
            f"Questions? Just reply to this email."
        )
    else:
        # Mass-template letter (already carries greeting, attachments prose and
        # the sign-off) + the figures table as the receipt beneath it.
        body_html = f"{test_banner}{pay_cta_html}{letter_html}{figures_table}"
        body_text = (
            f"{html_to_text(letter_html)}\n\n"
            f"Billing period: {period or '—'}\n"
            f"Your production: {kwh:,.0f} kWh\n"
            f"{amount_rows_text}{pay_cta_text}\n"
            f"Questions? Just reply to this email."
        )
    # White-label: offtaker sees THEIR operator name; skin is Array Operator sky.
    html = render_email_skin(
        preheader=(f"Pay {amount_str} · solar credit invoice" if pay_url
                   else f"Your solar credit invoice for {cust} is attached."),
        headline="Your solar credit invoice",
        intro_line=(period or cust),
        body_html=body_html,
        cta=skin_cta,
        footer_line=f"Solar credit invoice from {operator}.  ·  Questions? just reply to this email.",
        wordmark=operator,
        product="array_operator",
    )
    text = render_email_skin_text(
        headline="Your solar credit invoice",
        intro_line=(period or cust),
        body_text=body_text,
        cta=skin_cta,
        wordmark=operator,
        product="array_operator",
    )
    return subject, html, text


def deliver_subscription(db, sub, tenant, *, invoice_date: Optional[date] = None,
                         triggered_by: str = "manual", is_test: bool = False,
                         note: Optional[str] = None,
                         expected_period_label: Optional[str] = None,
                         gmp_pdf_override: Optional[bytes] = None,
                         force: bool = False) -> dict:
    """Generate + email one subscription's report. Stamps schedule fields on
    success. Returns a structured result dict (never raises for the common
    failure cases — surfaces them in the result instead).

    `note` is the operator's edited email body (from an approved draft); when
    present it leads the email above the figure table.

    `expected_period_label` pins the period the operator reviewed: if a newer
    utility bill has landed and the freshly-built invoice is for a different
    period, we refuse (period_changed) rather than silently sending the drift.

    `gmp_pdf_override` is the GMP bill PDF to attach for THIS send only (from an
    approved draft), passed through instead of being persisted onto the sub — so
    a once-attached bill never rides future periods' invoices.

    `force=True` bypasses the exactly-once-per-period guard for a deliberate
    operator re-send."""
    from ..notify import _send_via_resend

    try:
        match = build_match(sub)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"workbook unreadable: {e}"}
    if not match.matched or not match.latest_period:
        return {"ok": False, "error": "no current billing period in the stored workbook"}

    # ── Guard: don't send a period the operator didn't review (#3) ────────────
    # approve_draft rebuilds the invoice fresh here; if a newer utility bill landed
    # between drafting and approval, the customer would otherwise receive a
    # DIFFERENT period than the one reviewed. When the caller pins the reviewed
    # period, refuse (and prompt to regenerate) rather than sending the drift.
    _ci = match.computed_invoice or {}
    cur_period_label = None
    if _ci.get("period_start") or _ci.get("period_end"):
        cur_period_label = f"{_ci.get('period_start') or '—'} → {_ci.get('period_end') or '—'}"
    if expected_period_label is not None and cur_period_label != expected_period_label:
        return {"ok": False, "period_changed": True,
                "error": ("The utility bill changed since you reviewed this draft "
                          f"(reviewed {expected_period_label or '—'}; the current bill is "
                          f"{cur_period_label or '—'}). Regenerate the draft to review the "
                          "current period, then send.")}

    # ── Guard: exactly-once per billing period (#5) ───────────────────────────
    # Neither the scheduler nor a manual re-run may bill the same period twice. A
    # late GMP bill (build_match returns last month's) or an ops re-run would
    # otherwise re-send with a fresh invoice number. Skip when this exact period
    # was already sent to this offtaker, unless the caller explicitly forces it.
    cur_period_key = (_ci.get("period_end") or cur_period_label or None)
    if (not is_test) and (not force) and cur_period_key and \
            getattr(sub, "last_sent_period_end", None) == cur_period_key:
        return {"ok": False, "skipped": True, "already_sent": True,
                "error": (f"This offtaker's invoice for {cur_period_key} was already "
                          "sent — not sending a duplicate for the same period."),
                "invoice_number": _ci.get("invoice_number"),
                "amount_owed": _ci.get("amount_owed")}

    # ── OFFTAKER ↔ UTILITY BILL guardrail (Ford's rule) ───────────────────────
    # A typed/manual offtaker (no uploaded workbook) is invoiced EXCLUSIVELY from
    # the GMP utility PAPER BILL. We only EMAIL when the computed invoice resolved
    # to a real utility bill. Two ways it can fail to:
    #   • bound to a GMP account but no bill has landed for this period yet
    #     (kwh_source == 'utility_bill' but has_utility_bill is False), or
    #   • never bound to a GMP account at all, so build_manual_match fell back to
    #     generation TELEMETRY (kwh_source 'daily_csv' / 'gmp_api' / a '+'-joined
    #     multi-array mix) — exactly the silent-wrong-invoice case.
    # In either case SKIP and wait rather than emailing a telemetry-based invoice.
    # Workbook subscriptions are exempt (they invoice from the operator's uploaded
    # spreadsheet by design). This gates only the SEND — build_manual_match still
    # renders the figures for previews/drafts; we just won't put them in an email.
    # The OFFTAKER<->UTILITY-BILL guard applies even to a TEST send: previewing a
    # telemetry-derived "invoice" (to the operator) still misrepresents the rule that
    # offtaker invoices come EXCLUSIVELY from the settled GMP paper bill. So if the
    # computed invoice didn't resolve to a real utility bill — bound to a GMP account
    # but this period's bill hasn't landed, OR never bound at all (only telemetry
    # available) — SKIP and wait, test or not. Workbook subscriptions are exempt.
    _ci_guard = match.computed_invoice or {}
    if not getattr(sub, "source_workbook", None):
        _src = _ci_guard.get("kwh_source")
        _has_bill = _ci_guard.get("has_utility_bill") is True
        # Is the bound account a VEC/SmartHub one? (Its measured-generation source
        # string is provenance-honest — e.g. 'daily_csv'/'smarthub' — so we can't
        # detect "VEC" from kwh_source alone; look up the account's provider.)
        _is_sh_bound = False
        _uaid = getattr(sub, "utility_account_id", None)
        if _uaid is not None:
            from ..models import UtilityAccount
            from ..adapters import is_smarthub_provider
            _ga = db.get(UtilityAccount, _uaid)
            _is_sh_bound = (
                is_smarthub_provider((_ga.provider or "").lower())
                if _ga else False)
        # A SENDABLE invoice is one built from a settled GMP utility bill OR from a
        # billable VEC/SmartHub measured-generation invoice (has_utility_bill flags
        # both). The has_utility_bill gate (real bill / real operator rate + real
        # generation) still does the heavy lifting — generation telemetry on an
        # unbound or GMP offtaker is never sendable.
        _sendable_src = (
            _src == "utility_bill"
            or (_src or "").startswith("smarthub")
            or _is_sh_bound)
        if not _sendable_src or not _has_bill:
            if _is_sh_bound:
                _reason = ("waiting on this VEC/SmartHub offtaker — set a net-"
                           "metering rate and confirm the array's generation has "
                           "landed (we never bill VEC at the GMP/VT default)")
            elif _uaid is not None:
                _reason = ("waiting on the utility bill for this offtaker — no GMP "
                           "bill has landed for this period yet (no vendor data is "
                           "substituted)")
            else:
                _reason = ("this offtaker isn't linked to a GMP utility bill, so "
                           "there is no settled bill to invoice from. Link it to a "
                           "GMP utility account to start sending — generation "
                           "telemetry is never substituted")
            return {"ok": False, "skipped": True, "error": _reason,
                    "kwh_source": _src}

    # For a real (non-test) send honor the slider; a test always goes to_me.
    if is_test:
        op = sub.operator_email or getattr(tenant, "contact_email", None)
        to, cc, problems = ([op] if op else []), [], (
            [] if op else ["No operator email on file for the test send."])
    else:
        to, cc, problems = resolve_recipients(sub, tenant)
    if not to:
        return {"ok": False, "error": "; ".join(problems) or "no recipients"}

    # The operator is BCC'd on every offtaker invoice so they ALWAYS get a copy of
    # exactly what the customer received — unless they're already a direct recipient
    # (to_me puts them in To, to_both cc's them) or this is a test (already to_me).
    op_bcc = sub.operator_email or getattr(tenant, "contact_email", None)
    bcc = [op_bcc] if (op_bcc and not is_test and op_bcc not in to and op_bcc not in cc) else []

    formats = sub.formats or ["pdf"]

    # V2 offtaker pay-link: mint a Stripe Checkout Session (destination charge +
    # platform application fee) when the owner has Connect ready. Best-effort —
    # a Stripe failure never blocks the classic invoice email.
    #
    # ALSO mint on test sends so the operator sees the same Pay button the
    # offtaker will (screenshot 2026-07-13: test send had no pay link because
    # we previously gated on not is_test).
    pay_url = None
    payment_id = None
    fee_cents = None
    pay_skip_reason = None
    if (getattr(tenant, "product", None) or "nepool") == "array_operator":
        try:
            from . import payments as _pay
            # Refresh / auto-link Connect before minting so a just-finished bank
            # setup (or a Connect account finished under another tenant row with
            # the same email) is picked up without a page reload.
            try:
                if not getattr(tenant, "stripe_connect_account_id", None):
                    _pay.link_existing_connect_account(db, tenant)
                _pay.refresh_connect_status(db, tenant)
                db.refresh(tenant)
            except Exception:  # noqa: BLE001
                logger.warning("connect status refresh failed for %s",
                               getattr(tenant, "id", "?"), exc_info=True)
            pay_res = _pay.create_offtaker_payment(
                db, tenant=tenant, sub=sub, match=match, force=force)
            if pay_res.get("ok") and pay_res.get("pay_url"):
                pay_url = pay_res["pay_url"]
                payment_id = pay_res.get("payment_id")
                fee_cents = pay_res.get("fee_cents")
            else:
                pay_skip_reason = pay_res.get("error") or "pay link not created"
                if not pay_res.get("skipped"):
                    logger.warning("offtaker pay-link skipped for sub=%s: %s",
                                   getattr(sub, "id", "?"), pay_skip_reason)
        except Exception as e:  # noqa: BLE001 — never sink a send on pay-link bugs
            pay_skip_reason = f"pay-link error: {e}"
            logger.exception("offtaker pay-link creation crashed for sub=%s",
                             getattr(sub, "id", "?"))

    with tempfile.TemporaryDirectory(prefix="ao-bill-") as tmp:
        try:
            paths = generate_files(match, formats, sub.include_summary,
                                   pathlib.Path(tmp), invoice_date=invoice_date,
                                   sub=sub, gmp_pdf_override=gmp_pdf_override,
                                   pay_url=pay_url)
        except Exception as e:  # noqa: BLE001
            logger.exception("billing render failed")
            return {"ok": False, "error": f"render failed: {e}"}
        attachments = [_b64(p) for p in paths]
        subject, html, text = _email_html(match, sub, is_test, note=note,
                                          attachment_names=[p.name for p in paths],
                                          pay_url=pay_url)

        # White-label the sender: the offtaker sees the OPERATOR, not Array
        # Operator. Send under the operator's name — from their own verified
        # sending domain if they configured one, else the platform's sending
        # address carrying their display name — and route replies to the operator.
        product = getattr(tenant, "product", "array_operator")
        op_name = _operator_company_name(getattr(tenant, "id", None)) or getattr(tenant, "name", None)
        op_email = getattr(tenant, "contact_email", None)
        if getattr(tenant, "send_from_email", None):
            from_addr = (f'"{op_name}" <{tenant.send_from_email}>' if op_name else tenant.send_from_email)
        elif op_name:
            from_addr = f'"{op_name}" <{_platform_from_email(product)}>'
        else:
            from_addr = None

        ok = _send_via_resend(
            to=to[0] if len(to) == 1 else to, subject=subject, html=html, text=text,
            attachments=attachments, cc=cc or None, bcc=bcc or None, from_addr=from_addr,
            reply_to=(op_email or None), product=product,
        )
        # Capture Resend id immediately after send (bool return is back-compat;
        # id lives on the function attr / last_resend_id helper).
        from ..notify import last_resend_id as _last_resend_id
        resend_email_id = _last_resend_id() if ok else None

    result = {"ok": bool(ok), "to": to, "cc": cc, "bcc": bcc,
              "attachments": [p.name for p in paths],
              "invoice_number": (match.computed_invoice or {}).get("invoice_number"),
              "amount_owed": (match.computed_invoice or {}).get("amount_owed"),
              "triggered_by": triggered_by, "test": is_test,
              "pay_url": pay_url, "payment_id": payment_id, "fee_cents": fee_cents,
              "pay_skip_reason": pay_skip_reason,
              # Mailer accepted — NOT inbox-delivered. Webhook stamps delivered.
              "resend_email_id": resend_email_id if ok else None}
    if ok:
        result["delivery_status"] = "accepted"
    if ok and not is_test:
        now = datetime.utcnow()
        sub.last_sent_at = now
        sub.last_invoice_number = result["invoice_number"]
        # Dollars of the invoice just sent — the send-pipeline dashboard sums
        # these for the delivered-$ roll-up (never rebuilt per-sub at read time).
        try:
            sub.last_sent_amount_usd = (float(result["amount_owed"])
                                        if result.get("amount_owed") is not None else None)
        except (TypeError, ValueError):
            sub.last_sent_amount_usd = None
        # Record the period just sent so the exactly-once guard (#5) can block a
        # duplicate send of the same billing period (late bill / ops re-run).
        if cur_period_key:
            sub.last_sent_period_end = cur_period_key
        # Sequential numbering: this number is now used — advance the counter so the
        # next invoice gets start+1, start+2, …
        if getattr(sub, "invoice_number_next", None) is not None:
            sub.invoice_number_next = sub.invoice_number_next + 1
        sub.next_send_at = next_send_at(sub.cadence, now)
        # Delivery-truth: stamp Resend id so webhook can match by email_id.
        # Do NOT set last_delivered_at here — that requires email.delivered.
        if resend_email_id:
            sub.last_resend_email_id = str(resend_email_id)[:64]
        db.commit()
    # is_test: return resend_email_id in result (above) but never stamp delivery
    # fields on the subscription row.
    if not ok:
        result["error"] = "email send failed (check RESEND_API_KEY / domain)"
    return result


def _operator_review_email(sub, tenant, draft) -> tuple[str, str, str]:
    """The 'a report is ready for your review' note sent to the OPERATOR when a
    scheduled period lands in approval mode. It does NOT contain the invoice —
    it points the operator at their inbox to review, optionally edit, and send."""
    from ..email_skin import render_email_skin, render_email_skin_text
    from ..branding import app_url  # product-aware app origin
    cust = sub.customer_name or "your customer"
    amt = draft.amount_usd
    amt_str = f"${amt:,.2f}" if isinstance(amt, (int, float)) else "—"
    try:
        url = app_url(getattr(tenant, "product", "array_operator")).rstrip("/") + f"/?draft={sub.id}#reports"
    except Exception:  # noqa: BLE001
        url = f"https://arrayoperator.com/?draft={sub.id}#reports"
    subject = f"Ready to review — {cust} solar report"
    body_html = (
        f"<p>A new solar report for <strong>{cust}</strong> is drafted and waiting "
        f"in your approval inbox.</p>"
        f'<table width="100%" style="font-size:14px;border-collapse:collapse;margin-top:8px;">'
        f'<tr><td style="padding:6px 0;opacity:.65;">Period</td>'
        f'<td style="padding:6px 0;text-align:right;">{draft.period_label or "—"}</td></tr>'
        f'<tr><td style="padding:6px 0;opacity:.65;">Their production</td>'
        f'<td style="padding:6px 0;text-align:right;">{(draft.customer_kwh or 0):,.0f} kWh</td></tr>'
        f'<tr><td style="padding:10px 0;opacity:.65;">Amount</td>'
        f'<td style="padding:10px 0;text-align:right;font-weight:700;color:#3fd68a;">{amt_str}</td></tr>'
        f"</table>"
        f'<p style="margin-top:18px;"><a href="{url}" '
        f'style="background:#16a34a;color:#fff;padding:11px 20px;border-radius:8px;'
        f'text-decoration:none;font-weight:600;display:inline-block;">Review &amp; send</a></p>'
        f'<p style="margin-top:14px;font-size:13px;opacity:.7;">Nothing has been sent to your '
        f"customer. Open the report, edit anything you like, then approve to send it.</p>"
    )
    html = render_email_skin(
        preheader=f"A solar report for {cust} is ready for your review.",
        intro_line=f"Ready to review — {cust}",
        body_html=body_html, product="array_operator")
    text = render_email_skin_text(
        intro_line=f"Ready to review — {cust}",
        body_text=(
            f"A solar report for {cust} is drafted and waiting in your approval inbox.\n\n"
            f"Period: {draft.period_label or '—'}\n"
            f"Their production: {(draft.customer_kwh or 0):,.0f} kWh\n"
            f"Amount: {amt_str}\n\n"
            f"Review, edit, and send it here: {url}\n\n"
            f"Nothing has been sent to your customer yet."
        ),
        product="array_operator")
    return subject, html, text


def draft_subscription(db, sub, tenant, *, triggered_by: str = "scheduled") -> dict:
    """Approval-mode handling for a due scheduled period: create (or reuse) a
    pending ReportDraft from the stored workbook and email the OPERATOR a
    'ready to review' note. The report lands in their inbox — they open it,
    optionally edit, and click Approve & send. Nothing reaches the customer here.

    Stamps next_send_at forward so the same period isn't re-drafted every tick.
    Returns a structured result dict (never raises for common failures).
    """
    from ..models import ReportDraft
    from ..notify import _send_via_resend

    try:
        match = build_match(sub)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"workbook unreadable: {e}"}
    if not match.matched or not match.latest_period:
        return {"ok": False, "error": "no current billing period in the stored workbook"}

    ci = match.computed_invoice or {}

    # ── HOLD, don't draft, while waiting on the utility bill ─────────────────
    # A bill-bound offtaker whose period isn't billable yet — no settled bill,
    # or a QUARTERLY invoice held because one of the quarter's months is still
    # missing — must not land a misleading $0 "ready to review" draft in the
    # operator's inbox. Skip WITHOUT stamping next_send_at, so the scheduler
    # retries each tick and the real draft appears the moment the bill lands
    # (mirrors deliver_subscription's send guard). Workbook subs and legacy
    # unbound generation subs keep their existing draft behavior.
    if (not getattr(sub, "source_workbook", None)
            and getattr(sub, "utility_account_id", None) is not None
            and ci.get("has_utility_bill") is not True):
        return {"ok": False, "skipped": True,
                "error": ("; ".join(match.warnings or [])
                          or "waiting on the utility bill for this period"),
                "triggered_by": triggered_by}

    # ── exactly-once for DRAFTS too (caught at 800-offtaker scale) ───────────
    # deliver_subscription refuses to re-SEND an already-sent period, but until
    # a NEW bill lands the freshly-built invoice is still for that same period —
    # without this mirror every scheduler tick after an approval re-drafts a
    # phantom "ready to review" (at Anna scale: hundreds on the 1st of every
    # month) whose approval the send guard would then refuse anyway. Skip
    # WITHOUT stamping next_send_at so the real draft appears the moment the
    # next bill lands (same retry semantics as the bill-hold above).
    _pe_key = ci.get("period_end")
    if _pe_key and getattr(sub, "last_sent_period_end", None) == _pe_key:
        return {"ok": False, "skipped": True, "already_sent": True,
                "error": (f"The invoice for {_pe_key} was already sent — waiting "
                          "on the next utility bill before drafting again."),
                "triggered_by": triggered_by}

    inv_no = ci.get("invoice_number")
    period_label = None
    if ci.get("period_start") or ci.get("period_end"):
        period_label = f"{ci.get('period_start') or '—'} → {ci.get('period_end') or '—'}"
    cust_kwh = ci.get("kwh")
    pct = match.allocation_pct
    array_total = ci.get("project_total_kwh") or ci.get("array_kwh")
    if array_total is None and cust_kwh is not None and pct:
        array_total = round(cust_kwh / pct, 1)

    # Idempotent per (subscription, billing PERIOD) — keyed off the stable
    # period_label, NOT invoice_number. The invoice number changes on regeneration
    # (e.g. a bumped sequence or a different period_end), which let a regenerated
    # draft DUPLICATE for the same period; period_label (period_start → period_end)
    # is stable for a given billing period, so we update the one draft in place.
    existing = None
    if period_label is not None:
        existing = db.execute(
            select(ReportDraft).where(
                ReportDraft.subscription_id == sub.id,
                ReportDraft.status == "pending",
                ReportDraft.period_label == period_label,
            )
        ).scalars().first()
    draft = existing or ReportDraft(
        tenant_id=sub.tenant_id, subscription_id=sub.id,
        customer_name=sub.customer_name, status="pending")
    draft.period_label = period_label
    draft.array_total_kwh = array_total
    draft.allocation_pct = pct
    draft.customer_kwh = cust_kwh
    draft.amount_usd = ci.get("amount_owed")
    draft.invoice_number = inv_no
    if existing is None:
        db.add(draft)
    # Move the schedule forward so we don't re-draft this period next tick.
    now = datetime.utcnow()
    sub.next_send_at = next_send_at(sub.cadence, now)
    db.commit()

    # Notify the operator (best-effort — the draft is created regardless).
    op = sub.operator_email or getattr(tenant, "contact_email", None)
    notified = False
    if op:
        try:
            subject, html, text = _operator_review_email(sub, tenant, draft)
            from_addr = None
            if getattr(tenant, "send_from_email", None):
                nm = getattr(tenant, "send_from_name", None) or getattr(tenant, "company_name", None)
                from_addr = f'"{nm}" <{tenant.send_from_email}>' if nm else tenant.send_from_email
            notified = bool(_send_via_resend(
                to=op, subject=subject, html=html, text=text,
                from_addr=from_addr, product="array_operator"))
        except Exception:  # noqa: BLE001
            notified = False
    return {"ok": True, "drafted": True, "draft_id": draft.id,
            "operator_notified": notified, "to_review": op,
            "triggered_by": triggered_by}
